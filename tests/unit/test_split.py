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
    # every part keeps containment + span, and is flagged partial
    assert all(p.parentId == "f.py" and p.startLine == 1 for p in parts)
    assert all(p.isPartial is True for p in parts)


def test_split_leaves_ispartial_absent_when_not_split() -> None:
    parts = split_oversized_statements([_stmt("small")], 8000)
    assert parts[0].isPartial is None  # honest-null → excluded from NDJSON


def test_split_keeps_semantics_on_first_part_only() -> None:
    # A single source statement must not fan out into N duplicate route nodes.
    st = _stmt("x" * 17000, semanticType="route", method="GET", endpoint="/a", name="h")
    parts = split_oversized_statements([st], 8000)
    assert parts[0].semanticType == "route" and parts[0].method == "GET"
    assert parts[0].endpoint == "/a" and parts[0].name == "h"
    for cont in parts[1:]:
        assert cont.semanticType is None and cont.method is None
        assert cont.endpoint is None and cont.name is None


# ── max_statement_parts cap ──────────────────────────────────────────────────
def test_split_within_cap_is_lossless() -> None:
    # 17000 chars → 3 parts; a cap of 5 is not reached, so nothing is dropped.
    st = _stmt("x" * 17000)
    parts = split_oversized_statements([st], 8000, max_parts=5)
    assert len(parts) == 3
    assert "".join(p.text for p in parts) == "x" * 17000  # still lossless


def test_split_caps_parts_and_marks_dropped_tail() -> None:
    # 50000 chars → 7 full parts; cap at 3 keeps 3 and drops the tail with a marker.
    st = _stmt("x" * 50000)
    parts = split_oversized_statements([st], 8000, max_parts=3)
    assert [p.id for p in parts] == [
        "f.py:1:0#part1of3", "f.py:1:0#part2of3", "f.py:1:0#part3of3",
    ]
    assert all(len(p.text) <= 8000 for p in parts)  # marker never pushes a part over the cap
    assert all(p.isPartial is True for p in parts)
    # the loss is honest: last part carries an inline drop marker naming the dropped count
    # (50000 - 24000 kept = 26000 dropped) and the cap that caused it
    assert "26000 chars dropped" in parts[-1].text
    assert "max_statement_parts=3" in parts[-1].text
    assert len("".join(p.text for p in parts)) < 50000  # data was dropped, not reassemblable


def test_split_cap_disabled_by_zero() -> None:
    st = _stmt("x" * 50000)
    parts = split_oversized_statements([st], 8000, max_parts=0)
    assert len(parts) == 7 and "".join(p.text for p in parts) == "x" * 50000  # unbounded, lossless


def test_split_logs_warning_when_capped() -> None:
    from structlog.testing import capture_logs
    st = _stmt("x" * 50000)
    with capture_logs() as logs:
        split_oversized_statements([st], 8000, max_parts=3)
    capped = [e for e in logs if e.get("event") == "statement.parts_capped"]
    assert len(capped) == 1  # the drop is not silent
    assert capped[0]["log_level"] == "warning"
    assert capped[0]["kept"] == 3 and capped[0]["parts"] == 7
    assert capped[0]["dropped_chars"] == 26000 and capped[0]["id"] == "f.py:1:0"


def test_split_no_warning_within_cap() -> None:
    from structlog.testing import capture_logs
    with capture_logs() as logs:
        split_oversized_statements([_stmt("x" * 17000)], 8000, max_parts=5)
    assert not [e for e in logs if e.get("event") == "statement.parts_capped"]


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
    assert all(p.isPartial is True for p in parts)  # every part flagged for the reader
    assert parts[0].semanticType == "structured_data"  # classification on part 1
    assert all(p.semanticType is None for p in parts[1:])  # ...not duplicated


def test_pipeline_caps_parts_via_settings(tmp_path) -> None:
    # max_statement_parts (env/Settings) bounds the part count end-to-end.
    repo = tmp_path / "repo"
    repo.mkdir()
    big = {"rows": [{"id": i, "v": "y" * 60} for i in range(200)]}
    (repo / "data.json").write_text(json.dumps(big))

    settings = Settings(
        _env_file=None, repo=repo, capture_statements=True,
        statement_text_limit=2000, max_statement_parts=3,
    )
    sink = MemorySink()
    pipeline.run(repo, settings, sink)

    parts = next(r for r in sink.records if r.path == "data.json").statements
    assert len(parts) == 3  # capped
    assert "chars dropped" in parts[-1].text  # honest tail marker
