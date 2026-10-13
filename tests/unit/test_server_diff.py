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
from breezeai_cog.server.git import INVALID_REPO_URL_MESSAGE, parse_repo_url

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
    assert payload["codeOntologyId"] == "C1" and payload["storage_key"] == out["storage_key"]


def test_incremental_diff_filters_to_changed(captured: _Captured) -> None:
    client = _make_client(captured, filter_set={"a.py"}, deleted=["old.py"])
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 200
    assert r.json()["deletedFiles"] == ["old.py"]
    assert {rec["path"] for rec in captured.records} == {"a.py"}  # only changed file streamed
    assert captured.notifications[0][1]["projectMetaData"]["totalFiles"] == 1


def test_config_files_are_not_analyzed_languages(captured: _Captured) -> None:
    # same rule as the full scan: config / data documents are counted, not listed as languages
    files = {
        "package.json": '{"name": "w", "dependencies": {"left-pad": "1.0.0"}}',
        "workflow.json": '{"nodes": [{"id": "1", "name": "a"}], "active": true}',
    }
    client = _make_client(captured, filter_set={"a.py", *files}, deleted=[], repo_files=files)
    assert client.post("/api/analyze-diff", json=BODY).status_code == 200
    assert {rec["path"] for rec in captured.records} == {"a.py", *files}  # configs still streamed
    meta = captured.notifications[0][1]["projectMetaData"]
    assert meta["analyzedLanguages"] == ["python"]
    assert meta["configs"]["totalConfigFiles"] == 2


def test_deletion_only_commit(captured: _Captured) -> None:
    client = _make_client(captured, filter_set=set(), deleted=["gone.py"])
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 200
    out = r.json()
    assert out["deletedFiles"] == ["gone.py"]
    assert "Deletion-only commit" in out["message"]
    assert captured.records == []  # nothing parsed
    assert captured.notifications[0][1]["projectMetaData"]["totalFiles"] == 0


def test_no_op_commit_reports_no_file_changes(captured: _Captured) -> None:
    """A merge commit whose compare touches nothing: not a deletion-only commit."""
    client = _make_client(captured, filter_set=set(), deleted=[])
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 200
    out = r.json()
    assert out["deletedFiles"] == []
    assert "No file changes" in out["message"]
    assert "Deletion-only" not in out["message"]
    assert captured.records == []
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
    assert r.json() == {"error": INVALID_REPO_URL_MESSAGE}


def test_azure_devops_repo_url() -> None:
    # `project` became its own key in 36ed49f (was packed into repo as "project/repo").
    # Full URL-grammar coverage lives in tests/unit/integrations/test_scm_repository.py.
    url = "https://dev.azure.com/my-org/my-project/_git/my-repo"
    assert parse_repo_url(url) == {
        "provider": "azure_devops",
        "owner": "my-org",
        "project": "my-project",
        "repo": "my-repo",
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


# --- failure reporting (BREEZEAI-520 / BREEZEAI-681) -------------------------------

class _BrokenInfra(_FakeInfra):
    """Storage that fails on the first part write or on close — the two places an
    S3 multipart upload can break after ``open_storage`` succeeded."""

    def __init__(self, c: _Captured, *, write_exc=None, close_exc=None) -> None:
        super().__init__(c)
        self._write_exc, self._close_exc = write_exc, close_exc

    def write_line(self, line: str) -> None:
        if self._write_exc is not None:
            raise self._write_exc
        super().write_line(line)

    def close(self) -> str:
        if self._close_exc is not None:
            raise self._close_exc
        return super().close()


def _make_failing_client(
    captured: _Captured, *, acquire_exc=None, storage_exc=None, write_exc=None, close_exc=None,
    changed: set[str] | None = None,
) -> TestClient:
    def acquire(settings, body):
        if acquire_exc is not None:
            raise acquire_exc
        d = Path(tempfile.mkdtemp(prefix="difftest-"))
        (d / "a.py").write_text("def f():\n    return 1\n")
        return str(d), changed, []

    def open_storage(key):
        if storage_exc is not None:
            raise storage_exc
        if write_exc is not None or close_exc is not None:
            return _BrokenInfra(captured, write_exc=write_exc, close_exc=close_exc)
        return _FakeInfra(captured)

    deps = ServerDeps(
        settings=Settings(),
        open_storage=open_storage,
        notify=lambda path, payload: captured.notifications.append((path, payload)),
        acquire_diff=acquire,
    )
    return TestClient(create_app(Settings(), deps), raise_server_exceptions=False)


def test_git_failure_returns_structured_error_with_step(captured: _Captured) -> None:
    """A bare RuntimeError from git.py used to become a body-less 500; the backend
    then stored only "Request failed with status code 500"."""
    client = _make_failing_client(captured, acquire_exc=RuntimeError("GitHub API 401: bad credentials"))
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 502
    assert r.json() == {
        "error": "Git acquisition failed: GitHub API 401: bad credentials",
        "failedStep": "git_acquire",
    }
    assert captured.notifications == []  # nothing to ingest, nothing announced


def test_git_failure_scrubs_credentials(captured: _Captured) -> None:
    client = _make_failing_client(
        captured, acquire_exc=RuntimeError("git clone failed: https://x:ghp_secret@github.com/a/b")
    )
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 502
    assert "ghp_secret" not in r.json()["error"]
    assert "//***:***@github.com/a/b" in r.json()["error"]


def test_typed_git_error_keeps_status_and_gains_step(captured: _Captured) -> None:
    from breezeai_cog.server.errors import ApiError

    client = _make_failing_client(captured, acquire_exc=ApiError("Unsupported git provider: svn", 400))
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 400
    assert r.json() == {"error": "Unsupported git provider: svn", "failedStep": "git_acquire"}


def test_storage_failure_reports_upload_step(captured: _Captured) -> None:
    client = _make_failing_client(captured, storage_exc=ValueError("bucket not configured"))
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 500
    assert r.json() == {"error": "Storage open failed: bucket not configured", "failedStep": "upload"}
    assert captured.notifications == []


def test_unexpected_git_error_is_500_not_502(captured: _Captured) -> None:
    """Only provider-side failures (RuntimeError / httpx) are the provider's fault. A
    local bug or disk failure keeps the step but is not reported as a bad gateway."""
    client = _make_failing_client(captured, acquire_exc=KeyError("incomingCommitId"))
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 500
    assert r.json() == {
        "error": "Git acquisition failed unexpectedly: 'incomingCommitId'",
        "failedStep": "git_acquire",
    }


def test_provider_transport_error_is_502(captured: _Captured) -> None:
    import httpx

    req = httpx.Request("GET", "https://api.github.com/x")
    client = _make_failing_client(captured, acquire_exc=httpx.ConnectError("dns", request=req))
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 502
    assert r.json()["failedStep"] == "git_acquire"


def test_storage_failure_during_streaming_reports_upload_step(captured: _Captured) -> None:
    """An S3 part upload that breaks inside write_line() used to be reported as
    parse_stream because run_diff_stream wrapped both. The sink now tags it."""
    client = _make_failing_client(
        captured, write_exc=OSError("multipart upload part 3 failed: SlowDown")
    )
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 500
    assert r.json() == {
        "error": "Upload failed: multipart upload part 3 failed: SlowDown",
        "failedStep": "upload",
    }
    assert captured.notifications == []


def test_close_failure_after_streaming_reports_upload_step(captured: _Captured) -> None:
    """close() finalises the multipart upload; it now runs in the route, not inside
    run_diff_stream, so its failure is the upload step too."""
    client = _make_failing_client(captured, close_exc=RuntimeError("CompleteMultipartUpload 500"))
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 500
    assert r.json() == {"error": "Upload failed: CompleteMultipartUpload 500", "failedStep": "upload"}
    assert captured.notifications == []


def test_close_failure_on_no_change_commit_reports_upload_step(captured: _Captured) -> None:
    client = _make_failing_client(
        captured, close_exc=RuntimeError("CompleteMultipartUpload 500"), changed=set()
    )
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 500
    assert r.json()["failedStep"] == "upload"


def test_parser_failure_still_reports_parse_stream(captured: _Captured, monkeypatch: pytest.MonkeyPatch) -> None:
    from breezeai_cog.server import routes as routes_mod

    def boom(*args, **kwargs):
        raise ValueError("tree-sitter grammar missing")

    monkeypatch.setattr(routes_mod, "run_diff_stream", boom)
    client = _make_failing_client(captured)
    r = client.post("/api/analyze-diff", json=BODY)
    assert r.status_code == 500
    assert r.json() == {
        "error": "Parse/stream failed: tree-sitter grammar missing",
        "failedStep": "parse_stream",
    }
