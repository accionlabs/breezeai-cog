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


class _FakeS3:
    def __init__(self, c: _Captured) -> None:
        self._c, self._lines = c, []

    def write_line(self, line: str) -> None:
        self._lines.append(line)

    def close(self) -> str:
        self._c.records.extend(json.loads(x) for x in self._lines)
        return "ok"


def _make_client(captured: _Captured, filter_set, deleted) -> TestClient:
    def acquire(settings, body):
        d = Path(tempfile.mkdtemp(prefix="difftest-"))
        (d / "a.py").write_text("def f():\n    return 1\n")
        (d / "b.py").write_text("class B:\n    def m(self):\n        return 2\n")
        return str(d), filter_set, deleted

    deps = ServerDeps(
        settings=Settings(),
        open_s3=lambda key: _FakeS3(captured),
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
    assert out["success"] and out["s3Key"] == "code-ontology/P1/abc123.ndjson.gz"
    assert out["deletedFiles"] == []
    assert {rec["path"] for rec in captured.records} == {"a.py", "b.py"}  # all files streamed
    path, payload = captured.notifications[0]
    assert path == "/code-ontology/stream-ingest"
    assert "llmPlatform" not in payload  # accepted deviation: dropped
    meta = payload["projectMetaData"]
    assert meta["repoUrl"] == BODY["repoUrl"] and meta["commitId"] == "abc123"
    assert meta["gitBranch"] == "main" and meta["totalFiles"] == 2
    assert payload["codeOntologyId"] == "C1" and payload["s3Key"] == out["s3Key"]


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
    r = client.post("/api/analyze-diff", json={**BODY, "repoUrl": "https://gitlab.com/a/b"})
    assert r.status_code == 400
    assert r.json() == {"error": "Invalid repo URL (supported hosts: github.com, bitbucket.org)"}
