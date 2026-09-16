"""The capture ``id``/``parentId`` convention — defined once,
used by parsers when building records. The position suffix keeps anonymous
functions, overloads, and same-line statements from colliding.

    File      -> path
    Class     -> path#ClassName            (nested: path#Outer.Inner)
    Function  -> path#[Class#]name@startLine   (name='<anonymous>' if unnamed)
    Statement -> path:startLine:startCol

A statement whose ``text`` exceeds the configured cap is split at emit into ordered
parts, each id gaining a ``#part{i}of{n}`` suffix (``path:startLine:startCol#part1of3``)
— see :mod:`breezeai_cog.emit.split`.

The backend stores this id verbatim as the Neo4j ``Statement.captureId`` property (its
unique MERGE key) and also uses ``parentId`` to wire containment edges. It only needs to
be unique within a file — :func:`disambiguate` adds a deterministic ordinal on the rare
residual clash (the ``path`` prefix makes it unique across the repo).
"""

from __future__ import annotations

from typing import Any

ANONYMOUS = "<anonymous>"


def file_id(path: str) -> str:
    return path


def class_id(path: str, name: str) -> str:
    return f"{path}#{name}"


def function_id(path: str, name: str | None, start_line: int, *, class_name: str | None = None) -> str:
    owner = f"{class_name}#" if class_name else ""
    return f"{path}#{owner}{name or ANONYMOUS}@{start_line}"


def statement_id(path: str, start_line: int, start_col: int) -> str:
    return f"{path}:{start_line}:{start_col}"


def disambiguate(candidate: str, seen: set[str]) -> str:
    """Return a unique id, appending ``#2``, ``#3``, … if ``candidate`` is taken.
    Records the result in ``seen``."""
    if candidate not in seen:
        seen.add(candidate)
        return candidate
    i = 2
    while f"{candidate}#{i}" in seen:
        i += 1
    unique = f"{candidate}#{i}"
    seen.add(unique)
    return unique


class SeenIds(set[str]):
    """Tracks seen statement IDs for deduplication and statement records by span."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.by_span: dict[tuple[str, int, int], Any] = {}

    def register(self, fid: str, start_byte: int, end_byte: int, record: Any) -> None:
        self.by_span[(fid, start_byte, end_byte)] = record

    def get_record(self, fid: str, start_byte: int, end_byte: int) -> Any | None:
        return self.by_span.get((fid, start_byte, end_byte))

    def find_by_span(
        self, fid: str, start_byte: int, end_byte: int, node: Any | None = None
    ) -> Any | None:
        rec = self.by_span.get((fid, start_byte, end_byte))
        if rec is not None:
            return rec
        if node is not None:
            p = getattr(node, "parent", None)
            while p is not None:
                rec = self.by_span.get(
                    (fid, getattr(p, "start_byte", -1), getattr(p, "end_byte", -1))
                )
                if rec is not None:
                    return rec
                if getattr(p, "type", None) == "expression_statement":
                    break
                p = getattr(p, "parent", None)
        return None


def find_statement_by_span(
    seen_ids: Any, fid: str, start_byte: int, end_byte: int, node: Any | None = None
) -> Any | None:
    """Look up whether a record already exists for (fid, start_byte, end_byte) via seen_ids."""
    if hasattr(seen_ids, "find_by_span"):
        return seen_ids.find_by_span(fid, start_byte, end_byte, node=node)
    if hasattr(seen_ids, "get_record"):
        rec = seen_ids.get_record(fid, start_byte, end_byte)
        if rec is not None:
            return rec
    if hasattr(seen_ids, "by_span") and isinstance(seen_ids.by_span, dict):
        rec = seen_ids.by_span.get((fid, start_byte, end_byte))
        if rec is not None:
            return rec
    if node is not None and hasattr(seen_ids, "by_span") and isinstance(seen_ids.by_span, dict):
        p = getattr(node, "parent", None)
        while p is not None:
            rec = seen_ids.by_span.get(
                (fid, getattr(p, "start_byte", -1), getattr(p, "end_byte", -1))
            )
            if rec is not None:
                return rec
            if getattr(p, "type", None) == "expression_statement":
                break
            p = getattr(p, "parent", None)
    return None


def register_statement_span(
    seen_ids: Any, fid: str, start_byte: int, end_byte: int, record: Any
) -> None:
    """Register a statement record under (fid, start_byte, end_byte) in seen_ids."""
    if hasattr(seen_ids, "register"):
        seen_ids.register(fid, start_byte, end_byte, record)
    elif hasattr(seen_ids, "by_span") and isinstance(seen_ids.by_span, dict):
        seen_ids.by_span[(fid, start_byte, end_byte)] = record

