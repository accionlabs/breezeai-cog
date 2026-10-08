"""Enclosing: the per-run smallest-span lookup detectors use to enrich or parent statements.
It must match the linear scan it replaced — smallest span wins, the earliest listed wins a
tie — and see statements a detector appends between lookups."""

from __future__ import annotations

from breezeai_cog.parsers.enclosing import Enclosing
from breezeai_cog.schemas import FileRecord, Function, Statement


def _stmt(sid: str, start: int, end: int) -> Statement:
    return Statement(id=sid, parentId="f", nodeType="expression_statement", text=sid,
                     startLine=start, endLine=end, path="a.ts")


def _fn(fid: str, start: int, end: int) -> Function:
    return Function(id=fid, parentId="file", path="a.ts", name=fid, type="function",
                    startLine=start, endLine=end)


def _record(statements: list[Statement], functions: list[Function] = ()) -> FileRecord:
    return FileRecord(id="file", path="a.ts", type="code", language="typescript", loc=1,
                      statements=statements, functions=list(functions))


def test_smallest_span_wins_and_first_listed_breaks_ties() -> None:
    rec = _record([_stmt("if", 1, 10), _stmt("call", 3, 3), _stmt("a", 5, 6), _stmt("b", 5, 6)])
    enc = Enclosing(rec)
    assert enc.statement(3).id == "call"
    assert enc.statement(2).id == "if"
    assert enc.statement(5).id == "a"  # same span as "b" → earliest listed
    assert enc.statement(11) is None


def test_appended_statement_is_visible_to_later_lookups() -> None:
    rec = _record([_stmt("if", 1, 10)])
    enc = Enclosing(rec)
    assert enc.statement(4).id == "if"
    rec.statements.append(_stmt("added", 4, 5))  # a detector adds one mid-loop
    assert enc.statement(4).id == "added"
    rec.statements.append(_stmt("same", 4, 5))  # equal span, appended later → keeps "added"
    assert enc.statement(5).id == "added"


def test_function_id_innermost_else_fallback() -> None:
    rec = _record([], [_fn("outer", 1, 20), _fn("inner", 5, 8)])
    enc = Enclosing(rec)
    assert enc.function_id(6, "file") == "inner"
    assert enc.function_id(12, "file") == "outer"
    assert enc.function_id(30, "file") == "file"
