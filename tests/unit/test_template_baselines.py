"""Template capture baselines: every sample project still captures what its baseline holds.

Each directory under ``fixtures/templates/`` is a small project for one template type. Its
capture (templates on) is compared with ``fixtures/template_baselines/<name>.ndjson`` by
``scripts/capture_compare.py``: a record or field that disappears fails the test, while added
or changed records are allowed. Rewrite the baselines with
``python scripts/capture_compare.py check --update`` only when a change is intended.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "capture_compare.py"
_spec = importlib.util.spec_from_file_location("capture_compare", _SCRIPT)
assert _spec is not None and _spec.loader is not None
capture_compare = importlib.util.module_from_spec(_spec)
sys.modules["capture_compare"] = capture_compare  # @dataclass looks its module up here
_spec.loader.exec_module(capture_compare)

PROJECTS = capture_compare.sample_projects()


def _file(path: str, statements: list[dict], **fields) -> dict:
    return {
        "id": path,
        "path": path,
        "language": "html",
        "functions": [],
        "classes": [],
        "statements": statements,
        **fields,
    }


def _stmt(line: int, node: str = "element", **fields) -> dict:
    return {
        "id": f"p:{line}:0",
        "parentId": "p",
        "nodeType": node,
        "text": "t",
        "startLine": line,
        "endLine": line,
        **fields,
    }


# ── the comparison ───────────────────────────────────────────────────────────────────────────


def test_identical_captures_have_no_differences() -> None:
    capture = [_file("a.html", [_stmt(1, name="form")])]
    diff = capture_compare.compare(capture, capture)
    assert diff.as_dict() == {"lost": [], "added": [], "changed": []}


def test_a_missing_statement_is_lost() -> None:
    before = [_file("a.html", [_stmt(1, name="form"), _stmt(2, name="input")])]
    after = [_file("a.html", [_stmt(1, name="form")])]
    diff = capture_compare.compare(before, after)
    assert len(diff.lost) == 1 and "a.html 2 input" in diff.lost[0]


def test_a_missing_file_and_a_missing_field_are_lost() -> None:
    before = [
        _file("a.html", [_stmt(1, semanticType="api_call", endpoint="/x")]),
        _file("b.html", []),
    ]
    after = [_file("a.html", [_stmt(1, semanticType="api_call")])]
    lost = capture_compare.compare(before, after).lost
    assert "file b.html" in lost
    assert any("field endpoint" in item for item in lost)


def test_a_new_statement_or_field_is_added_not_lost() -> None:
    before = [_file("a.html", [_stmt(1, name="form")])]
    after = [_file("a.html", [_stmt(1, name="form", handler="save"), _stmt(2, name="input")])]
    diff = capture_compare.compare(before, after)
    assert diff.lost == []
    assert len(diff.added) == 2


def test_a_new_node_type_or_id_is_a_change_not_a_loss() -> None:
    before = [_file("a.html", [_stmt(1, node="synthetic", name="Register")])]
    moved = _stmt(1, node="directive", name="Register")
    moved["id"] = "p:1:4"
    diff = capture_compare.compare(before, [_file("a.html", [moved])])
    assert diff.lost == [] and diff.added == []
    assert diff.changed == ['statement a.html 1 Register: nodeType "synthetic" -> "directive"']


def test_a_lost_list_item_is_reported() -> None:
    before = [_file("a.html", [], importFiles=["b.html", "c.html"])]
    after = [_file("a.html", [], importFiles=["b.html"])]
    lost = capture_compare.compare(before, after).lost
    assert lost == ['file a.html: importFiles lost ["c.html"]']


def test_a_record_on_another_line_has_moved_not_been_lost() -> None:
    before = [_file("a.html", [_stmt(1, name="form"), _stmt(5, name="input")])]
    after = [_file("a.html", [_stmt(4, name="input")])]
    diff = capture_compare.compare(before, after)
    assert diff.lost == ["statement a.html 1 form [element]"]
    assert diff.added == []
    assert diff.changed == ["statement a.html 5 input: moved to line 4"]


# ── the sample projects ──────────────────────────────────────────────────────────────────────


def test_every_sample_project_has_a_baseline() -> None:
    assert PROJECTS, "no sample projects found"
    missing = [p.name for p in PROJECTS if not capture_compare.baseline_path(p).exists()]
    assert missing == []


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p.name)
def test_sample_project_loses_nothing_against_its_baseline(project: Path) -> None:
    baseline = capture_compare.load(capture_compare.baseline_path(project))
    diff = capture_compare.compare(baseline, capture_compare.capture(project))
    assert diff.lost == [], capture_compare.report(diff, title=project.name)
