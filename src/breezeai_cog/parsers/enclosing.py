"""Smallest enclosing statement / function for a line — the lookup a call-based detector uses
to enrich the statement a detected call sits in, or to parent a statement it appends.

Detectors ask once per detected call. Scanning ``record.statements`` on each ask made detection
O(hits × statements) — the per-item scan the pass budget forbids (parser review guide §6.1). An
:class:`Enclosing` answers from a line → smallest-span table built once, lazily (a file with no
hits pays nothing). Building it touches each line of each span once, so it costs the total line
span of the items: linear in file size for real nesting depths.

Semantics are those of the linear scan it replaces: the item with the smallest
``endLine - startLine`` whose span contains the line, the earliest in list order on a tie.
Statements a detector appends between asks are folded in on the next ask, so a just-added
statement can be found (and enriched) by a later call on its span, as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Generic, Protocol, TypeVar

from ..schemas import FileRecord, Function, Statement


class _Span(Protocol):
    startLine: int
    endLine: int


T = TypeVar("T", bound=_Span)


class _SmallestSpan(Generic[T]):
    """line → smallest-span item covering it, over a list that may grow by appends."""

    __slots__ = ("_items", "_done", "_best")

    def __init__(self, items: Sequence[T]) -> None:
        self._items = items
        self._done = 0
        self._best: dict[int, tuple[int, T]] = {}  # line → (span, item)

    def at(self, line: int) -> T | None:
        items, best = self._items, self._best
        for item in items[self._done :]:  # fold in anything appended since the last ask
            span = item.endLine - item.startLine
            for ln in range(item.startLine, item.endLine + 1):
                cur = best.get(ln)
                if cur is None or span < cur[0]:  # strict: the earlier item keeps a tie
                    best[ln] = (span, item)
        self._done = len(items)
        hit = best.get(line)
        return hit[1] if hit is not None else None


class Enclosing:
    """Per-detector-run lookups over one ``record``. Create once per detection pass, before
    its loop over calls; it follows appends to ``record.statements`` made meanwhile."""

    __slots__ = ("_record", "_statements", "_functions")

    def __init__(self, record: FileRecord) -> None:
        self._record = record
        self._statements: _SmallestSpan[Statement] | None = None
        self._functions: _SmallestSpan[Function] | None = None

    def statement(self, line: int) -> Statement | None:
        """The smallest already-captured statement spanning ``line`` (to enrich in place)."""
        if self._statements is None:
            self._statements = _SmallestSpan(self._record.statements)
        return self._statements.at(line)

    def function_id(self, line: int, fallback: str) -> str:
        """Id of the smallest function spanning ``line`` (parent for an appended statement),
        else ``fallback``."""
        if self._functions is None:
            self._functions = _SmallestSpan(self._record.functions)
        fn = self._functions.at(line)
        return fn.id if fn is not None else fallback
