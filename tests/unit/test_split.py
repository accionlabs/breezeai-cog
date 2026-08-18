"""Oversized-statement splitting — the ``emit.split`` unit surface plus an
end-to-end check that the pipeline splits a large captured document so nothing
is dropped at the backend's size cap."""

from __future__ import annotations

import json

from breezeai_cog.config import Settings
from breezeai_cog.core import pipeline
from breezeai_cog.emit import split_oversized_statements
from breezeai_cog.emit.sinks import MemorySink
from breezeai_cog.emit.split import _chunks
from breezeai_cog.schemas import Statement


def _stmt(text: str, **kw) -> Statement:
    base = dict(
        id="f.py:1:0", parentId="f.py", nodeType="expression_statement",
        text=text, startLine=1, endLine=1, path="f.py",
    )
    base.update(kw)
    return Statement(**base)


# ── _chunks ──────────────────────────────────────────────────────────────────
def test_chunks_hard_cut_when_no_newline() -> None:
    parts = _chunks("a" * 20000, 8000)
    assert [len(p) for p in parts] == [8000, 8000, 4000]
    assert "".join(parts) == "a" * 20000  # lossless


def test_chunks_break_on_last_newline_in_window() -> None:
    text = "l\n" + "b" * 7999 + "\n" + "c" * 100
    parts = _chunks(text, 8000)
    assert all(len(p) <= 8000 for p in parts)
    assert "".join(parts) == text
    assert parts[0] == "l\n"  # broke at the newline, not a hard cut mid-line


def test_chunks_within_limit_is_single_piece() -> None:
    assert _chunks("short", 8000) == ["short"]


# ── split_oversized_statements ───────────────────────────────────────────────
def test_split_leaves_small_statements_untouched() -> None:
    stmts = [_stmt("small"), _stmt("also small")]
    assert split_oversized_statements(stmts, 8000) is stmts  # same list, no copy


def test_split_disabled_when_limit_zero() -> None:
    stmts = [_stmt("x" * 20000)]
    assert split_oversized_statements(stmts, 0) is stmts


def test_split_produces_ordered_parts_that_reassemble() -> None:
    st = _stmt("x" * 17000, semanticType="route", method="GET", endpoint="/a", name="h")
    parts = split_oversized_statements([st], 8000)
    assert [p.id for p in parts] == [
        "f.py:1:0#part1of3", "f.py:1:0#part2of3", "f.py:1:0#part3of3",
    ]
    assert all(len(p.text) <= 8000 for p in parts)
    assert "".join(p.text for p in parts) == "x" * 17000  # lossless
    # every part keeps containment + span
    assert all(p.parentId == "f.py" and p.startLine == 1 for p in parts)


def test_split_keeps_semantics_on_first_part_only() -> None:
    # A single source statement must not fan out into N duplicate route nodes.
    st = _stmt("x" * 17000, semanticType="route", method="GET", endpoint="/a", name="h")
    parts = split_oversized_statements([st], 8000)
    assert parts[0].semanticType == "route" and parts[0].method == "GET"
    assert parts[0].endpoint == "/a" and parts[0].name == "h"
    for cont in parts[1:]:
        assert cont.semanticType is None and cont.method is None
        assert cont.endpoint is None and cont.name is None


# ── end-to-end through the pipeline ──────────────────────────────────────────
def test_pipeline_splits_large_captured_document(tmp_path) -> None:
    # A JSON data document is captured as ONE statement; with a small cap it must be
    # split into ordered parts (none exceeding the cap) rather than dropped whole.
    repo = tmp_path / "repo"
    repo.mkdir()
    big = {"rows": [{"id": i, "v": "y" * 60} for i in range(200)]}
    (repo / "data.json").write_text(json.dumps(big))

    settings = Settings(_env_file=None, repo=repo, capture_statements=True, statement_text_limit=2000)
    sink = MemorySink()
    pipeline.run(repo, settings, sink)

    rec = next(r for r in sink.records if r.path == "data.json")
    parts = rec.statements
    assert len(parts) > 1  # the document was split
    assert all(len(p.text) <= 2000 for p in parts)  # every part fits the cap
    assert all(p.id.startswith("data.json:1:0#part") for p in parts)
    assert parts[0].semanticType == "structured_data"  # classification on part 1
    assert all(p.semanticType is None for p in parts[1:])  # ...not duplicated
