"""What a detector returns (:class:`Emit`) and the one place that writes it onto a record
(:func:`apply`).

A detector finds things; it does not edit ``record.statements``. Each finding is an
:class:`Emit`: the AST node it sits on and the :class:`~..schemas.Statement` fields it sets.
:func:`apply` owns the rules every detector used to repeat:

* **Enrich or append.** An emit that allows enriching (``enrich``) writes its fields onto the
  smallest statement already captured on its line, when that statement is eligible. Otherwise
  it appends a new statement on the node's span.
* **Once per run.** A statement enriched or appended by this run is never enriched again by the
  same run: a second route in one statement (``route(p).get(a).post(b)``) gets its own record
  instead of overwriting the first.
* **Ids.** A new statement's id is ``path:line:col``, disambiguated (``#2``) against every id
  already on the record, in emit order. Emit order is therefore part of the output.
* **Parent.** A new statement's parent is ``parent`` when given, else the smallest function on
  its line, else the file.

::

    found = [Emit(call, {"semanticType": "route", "endpoint": "/users"},
                  fill={"handler": name}, enrich="any")]
    apply(record, found, source, path)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from tree_sitter import Node

from ..emit import disambiguate, file_id, statement_id
from ..schemas import FileRecord, Statement
from .enclosing import Enclosing
from .treesitter import first_line, node_text

#: Which captured statement an emit may enrich in place:
#: ``never`` — always append; ``unclassified`` — one with no ``semanticType`` (or one of
#: ``reclassify``); ``any`` — whatever statement is there.
Enrich = Literal["never", "unclassified", "any"]


@dataclass(slots=True)
class Emit:
    """One statement a detector found."""

    #: The construct. Its span gives the new statement's line, column, end line, ``nodeType``
    #: and ``text`` unless overridden below.
    node: Node
    #: Statement fields, always written (enriching or appending).
    fields: dict[str, Any]
    #: Statement fields written when enriching only if truthy; appended as given.
    fill: dict[str, Any] = field(default_factory=dict)
    enrich: Enrich = "never"
    #: With ``unclassified``: semantic types that may also be overwritten (a known mis-tag).
    reclassify: frozenset[str] = frozenset()
    #: Overrides for an appended statement.
    parent: str | None = None
    text: str | None = None
    node_type: str | None = None
    line: int | None = None
    col: int | None = None
    end_line: int | None = None

    @property
    def start_line(self) -> int:
        return self.line if self.line is not None else self.node.start_point[0] + 1


def _eligible(stmt: Statement, emit: Emit) -> bool:
    if emit.enrich == "any":
        return True
    return stmt.semanticType is None or stmt.semanticType in emit.reclassify


def apply(record: FileRecord, emits: list[Emit], source: bytes, path: str) -> None:
    """Write ``emits`` onto ``record`` in order (see the module docstring for the rules)."""
    if not emits:
        return
    seen = {s.id for s in record.statements}
    enclosing = Enclosing(record)
    done: set[str] = set()  # statements enriched or appended by this run
    fid = file_id(path)
    for emit in emits:
        line = emit.start_line
        stmt = enclosing.statement(line) if emit.enrich != "never" else None
        if stmt is not None and stmt.id not in done and _eligible(stmt, emit):
            done.add(stmt.id)
            for name, value in emit.fields.items():
                setattr(stmt, name, value)
            for name, value in emit.fill.items():
                if value:
                    setattr(stmt, name, value)
            continue
        node = emit.node
        col = emit.col if emit.col is not None else node.start_point[1]
        new_id = disambiguate(statement_id(path, line, col), seen)
        record.statements.append(Statement(
            id=new_id,
            parentId=emit.parent if emit.parent is not None else enclosing.function_id(line, fid),
            nodeType=emit.node_type or node.type,
            text=emit.text if emit.text is not None else first_line(node_text(node, source)),
            startLine=line,
            endLine=emit.end_line if emit.end_line is not None else node.end_point[0] + 1,
            path=path,
            **emit.fields,
            **emit.fill,
        ))
        done.add(new_id)
