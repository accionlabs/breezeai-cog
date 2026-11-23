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
* **Containing statement.** An emit with ``skip`` set looks for the statement to enrich among
  the captured statements that contain its node in the tree (the innermost one), not the
  smallest statement on its line: on a line holding two statements (``x = 1; call()``, ``with
  c as s: call()``, ``} catch (e) { call() }``) it is the one the call is in.

::

    found = [Emit(call, {"semanticType": "route", "endpoint": "/users"},
                  fill={"handler": name}, enrich="any")]
    apply(record, found, source, path)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from tree_sitter import Node

from ..emit import disambiguate, file_id, statement_id
from ..schemas import FileRecord, Statement
from .enclosing import Enclosing
from .treesitter import end_line, first_line, node_text

#: Which captured statement an emit may enrich in place:
#: ``never`` — always append; ``unclassified`` — one with no ``semanticType`` (or one of
#: ``reclassify``); ``own`` — an unclassified one that is the construct itself (exactly its line
#: span), so a call nested in a larger statement gets a record of its own; ``starts`` — an
#: unclassified one that begins on the construct's first line (``send(…).whenComplete(…)`` over
#: three lines is the send's statement; a send passed as an argument on a statement's second
#: line is not); ``same`` — the statement captured on this very node (same start line and
#: column, same node type), whatever its type — the base parser's record of the call itself;
#: ``any`` — whatever statement is there.
Enrich = Literal["never", "unclassified", "own", "starts", "same", "any"]


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
    #: Node types never enriched (a control-flow statement, whose span is its whole body): the
    #: emit is appended as its own statement instead. When set, the statement to enrich is the
    #: innermost captured statement that contains the node (see :func:`apply`); when that one is
    #: of a ``skip`` type (the node sits in a control-flow header), the emit is appended.
    skip: frozenset[str] = frozenset()
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

    @property
    def last_line(self) -> int:
        return self.end_line if self.end_line is not None else end_line(self.node)


def _eligible(stmt: Statement, emit: Emit) -> bool:
    if emit.enrich in ("any", "same"):
        return True
    if stmt.nodeType in emit.skip:
        return False
    if emit.enrich == "own" and (stmt.startLine, stmt.endLine) != (emit.start_line, emit.last_line):
        return False
    if emit.enrich == "starts" and stmt.startLine != emit.start_line:
        return False
    return stmt.semanticType is None or stmt.semanticType in emit.reclassify


#: ``path:line:col`` with an optional ``#n`` disambiguation suffix.
_ID_POSITION = re.compile(r":(\d+):(\d+)(?:#\d+)?$")

_Position = tuple[int, int, str]  # (line, column, node type) of a statement's node


def _position(stmt: Statement) -> _Position | None:
    m = _ID_POSITION.search(stmt.id)
    return (int(m.group(1)), int(m.group(2)), stmt.nodeType) if m else None


def _containing(node: Node, by_position: dict[_Position, Statement]) -> Statement | None:
    """The innermost captured statement whose node is ``node`` or one of its ancestors."""
    current: Node | None = node
    while current is not None:
        stmt = by_position.get((current.start_point[0] + 1, current.start_point[1], current.type))
        if stmt is not None:
            return stmt
        current = current.parent
    return None


def apply(record: FileRecord, emits: list[Emit], source: bytes, path: str) -> None:
    """Write ``emits`` onto ``record`` in order (see the module docstring for the rules)."""
    if not emits:
        return
    seen = {s.id for s in record.statements}
    enclosing = Enclosing(record)
    done: set[str] = set()  # statements enriched or appended by this run
    fid = file_id(path)
    by_id: dict[str, Statement] | None = None  # for ``same``, built on first use
    by_position: dict[_Position, Statement] | None = None  # for ``skip``, built on first use
    for emit in emits:
        line = emit.start_line
        if emit.enrich == "same":
            if by_id is None:
                by_id = {s.id: s for s in record.statements}
            col = emit.col if emit.col is not None else emit.node.start_point[1]
            stmt = by_id.get(statement_id(path, line, col))
            if stmt is not None and stmt.nodeType != emit.node.type:
                stmt = None
        elif emit.skip and emit.enrich != "never":
            if by_position is None:
                by_position = {}
                for s in record.statements:
                    pos = _position(s)
                    if pos is not None:
                        by_position.setdefault(pos, s)
            stmt = _containing(emit.node, by_position)
        else:
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
        fields = dict(emit.fields)
        last_line = fields.pop("endLine", emit.last_line)  # a record spanning several statements
        appended = Statement(
            id=new_id,
            parentId=emit.parent if emit.parent is not None else enclosing.function_id(line, fid),
            nodeType=emit.node_type or node.type,
            text=emit.text if emit.text is not None else first_line(node_text(node, source)),
            startLine=line,
            endLine=last_line,
            path=path,
            **fields,
            **emit.fill,
        )
        record.statements.append(appended)
        if by_position is not None:
            pos = _position(appended)
            if pos is not None:
                by_position.setdefault(pos, appended)
        done.add(new_id)
