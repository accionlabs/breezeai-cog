"""POST /api/analyze-diff — full-clone / incremental / deletion-only, S3 stream +
out-of-band projectMetaData notification, validation. Git acquisition is faked
(no network/clone); S3 + notify are in-memory."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from breezeai_cog.config import Settings
from breezeai_cog.server.app import create_app
from breezeai_cog.server.deps import ServerDeps
from breezeai_cog.server.git import parse_repo_url

BODY = {
    "repoUrl": "https://github.com/acme/widgets.git",
    "incomingCommitId": "abc123",
    "gitBranch": "main",
    "projectUuid": "P1",
    "codeOntologyId": "C1",
}


class _Captured:
    def __init__(self) -> None:
        self.records: list[dict] = []
        self.notifications: list[tuple[str, dict]] = []


class _FakeInfra:
    def __init__(self, c: _Captured) -> None:
        self._c, self._lines = c, []

    def write_line(self, line: str) -> None:
        self._lines.append(line)

    def close(self) -> str:
        self._c.records.extend(json.loads(x) for x in self._lines)
        return "ok"


def _make_client(captured: _Captured, filter_set, deleted, repo_files: dict | None = None) -> TestClient:
    def acquire(settings, body):
        d = Path(tempfile.mkdtemp(prefix="difftest-"))
        (d / "a.py").write_text("def f():\n    return 1\n")
        (d / "b.py").write_text("class B:\n    def m(self):\n        return 2\n")
        for name, content in (repo_files or {}).items():
            (d / name).write_text(content)
        return str(d), filter_set, deleted

    deps = ServerDeps(
        settings=Settings(),
        open_storage=lambda key: _FakeInfra(captured),
        notify=lambda path, payload: captured.notifications.append((path, payload)),
        acquire_diff=acquire,
    )
    return TestClient(create_app(Settings(), deps))


@pytest.fixture
def captured() -> _Captured:
    return _Captured()


def test_full_clone(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 200
    out = r.json()
    assert out["success"] and out["storage_key"] == "code-ontology/P1/abc123.ndjson.gz"
    assert out["deletedFiles"] == []
    assert {rec["path"] for rec in captured.records} == {"a.py", "b.py"}  # all files streamed
    path, payload = captured.notifications[0]
    assert path == "/code-ontology/stream-ingest"
    assert "llmPlatform" not in payload  # accepted deviation: dropped
    meta = payload["projectMetaData"]
    assert meta["repoUrl"] == BODY["repoUrl"] and meta["commitId"] == "abc123"
    assert meta["gitBranch"] == "main" and meta["totalFiles"] == 2
    assert payload["codeOntologyId"] == "C1" and payload["s3Key"] == out["storage_key"]


def test_incremental_diff_filters_to_changed(captured: _Captured) -> None:
    client = _make_client(captured, filter_set={"a.py"}, deleted=["old.py"])
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 200
    assert r.json()["deletedFiles"] == ["old.py"]
    assert {rec["path"] for rec in captured.records} == {"a.py"}  # only changed file streamed
    assert captured.notifications[0][1]["projectMetaData"]["totalFiles"] == 1


def test_deletion_only_commit(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=set(), deleted=["gone.py"])
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 200
    out = r.json()
    assert out["deletedFiles"] == ["gone.py"]
    assert "Deletion-only commit" in out["message"]
    assert captured.records == []  # nothing parsed
    assert captured.notifications[0][1]["projectMetaData"]["totalFiles"] == 0


def test_missing_fields(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json={"repoUrl": "https://github.com/a/b"})
    assert r.status_code == 400
    assert "All fields required" in r.json()["error"]


def test_invalid_repo_url(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json={**BODY, "repoUrl": "https://unsupported-host.com/a/b"})
    assert r.status_code == 400
    assert r.json() == {"error": "Invalid repo URL (supported hosts: github.com, bitbucket.org, gitlab.com, dev.azure.com)"}


def test_azure_devops_repo_url() -> None:
    url = "https://dev.azure.com/my-org/my-project/_git/my-repo"
    res = parse_repo_url(url)
    assert res == {
        "provider": "azure_devops",
        "owner": "my-org",
        "repo": "my-project/my-repo"
    }


# --- ignorePatterns (optional; additive to the built-ins and the repo's own files) ---


def test_ignore_patterns_exclude_matching_files(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": ["b.py"]})
    assert r.status_code == 200
    assert {rec["path"] for rec in captured.records} == {"a.py"}
    assert captured.notifications[0][1]["projectMetaData"]["totalFiles"] == 1


def test_ignore_patterns_accept_newline_string(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": "# comment\nb.py\n\n"})
    assert r.status_code == 200
    assert {rec["path"] for rec in captured.records} == {"a.py"}


def test_omitted_ignore_patterns_leave_behaviour_unchanged(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 200
    assert {rec["path"] for rec in captured.records} == {"a.py", "b.py"}


def test_blank_ignore_patterns_are_a_no_op(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": ["", "   "]})
    assert r.status_code == 200
    assert {rec["path"] for rec in captured.records} == {"a.py", "b.py"}


def test_pushed_patterns_append_to_repo_own_repoignore(captured: _Captured) -> None:
    """The repo's committed .repoignore must survive — both layers apply."""
    client = _make_client(captured, filter_set=None, deleted=[], repo_files={".repoignore": "a.py\n"})
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": ["b.py"]})
    assert r.status_code == 200
    assert captured.records == []  # a.py from the repo's file, b.py from the pushed patterns


def test_repoignore_without_trailing_newline_still_appends(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[], repo_files={".repoignore": "a.py"})
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": ["b.py"]})
    assert r.status_code == 200
    assert captured.records == []


def test_repoinclude_overrides_a_pushed_pattern(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[], repo_files={".repoinclude": "b.py\n"})
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": ["b.py"]})
    assert r.status_code == 200
    assert {rec["path"] for rec in captured.records} == {"a.py", "b.py"}


def test_ignore_patterns_apply_on_incremental_resync(captured: _Captured) -> None:
    client = _make_client(captured, filter_set={"a.py", "b.py"}, deleted=[])
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": ["b.py"]})
    assert r.status_code == 200
    assert {rec["path"] for rec in captured.records} == {"a.py"}


def test_all_changed_files_ignored_streams_empty_but_well_formed(captured: _Captured) -> None:
    """Zero records must still carry the full meta shape — empty_meta's bare
    ``configs`` {} would break a backend indexing configs.byType."""
    client = _make_client(captured, filter_set={"b.py"}, deleted=[])
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": ["b.py"]})
    assert r.status_code == 200
    assert captured.records == []
    meta = captured.notifications[0][1]["projectMetaData"]
    assert meta["totalFiles"] == 0
    assert "byType" in meta["configs"]


def test_ignore_patterns_reject_non_string_entries(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": ["ok", 7]})
    assert r.status_code == 400
    assert r.json()["error"] == "ignorePatterns[1] must be a string"


def test_ignore_patterns_reject_wrong_type(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json={**BODY, "ignorePatterns": {"a": 1}})
    assert r.status_code == 400
    assert r.json()["error"] == "ignorePatterns must be a string or an array of strings"


def test_notification_uses_s3Key_the_backend_dto_requires(captured: _Captured) -> None:
    """Regression for 4cf6484, which renamed this key to `storage_key` and made the
    backend's StreamIngestDto (`s3Key`, @IsNotEmpty) reject every callback with 400."""
    client = _make_client(captured, filter_set=None, deleted=[])
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 200
    _, payload = captured.notifications[0]
    assert payload["s3Key"] == "code-ontology/P1/abc123.ndjson.gz"
    assert "storage_key" not in payload
