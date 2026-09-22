"""POST /api/git-diff — the centralised provider diff (BREEZEAI-1228).

Covers the per-provider mapping ported from BreezeAI_Backend's `GitService`, the
`null`-not-`0` contract for values a provider cannot supply, and the two error
paths the ticket calls out (unknown host, provider failure). No network: the
httpx GET inside `server.git` is faked.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from breezeai_cog.server import git as git_mod
from breezeai_cog.server.app import create_app

HEAD = "01d9ad8afd9e6c6dc35882e947d86d357f1994a7"
BASE = "9f2c1ab4de7845bb0c3e6a1f8d2b7c4e5a6f8091"


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _Captured(list):
    """Recorded provider requests, plus the queue of replies to hand back."""

    def __init__(self) -> None:
        super().__init__()
        self.queue: list[_FakeResponse] = []


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    """Record every provider request and reply with a queued payload.

    `_diff_get` does `import httpx` inside the function, so the module attribute
    is what actually gets resolved at call time — patch that, not a local alias.
    TestClient drives the app through `httpx.Client`, not `httpx.get`, so it is
    unaffected.
    """
    import httpx

    calls = _Captured()

    def fake_get(url: str, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "params": params or {}})
        return calls.queue.pop(0) if calls.queue else _FakeResponse({})

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _post(client: TestClient, **body) -> Any:
    payload = {"repoUrl": "https://github.com/acme/widgets", "incomingCommitId": HEAD}
    payload.update(body)
    return client.post("/api/git-diff", json=payload)


# ── GitHub: the only provider with real counts AND patch ──────────────────────

def test_github_compare_maps_counts_and_patch(client, captured) -> None:
    captured.queue.append(_FakeResponse({"files": [
        {"filename": "src/app.ts", "status": "modified", "additions": 12, "deletions": 4, "patch": "@@ -1 +1 @@"},
    ]}))
    resp = _post(client, baseCommitId=BASE)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "baseSha": BASE, "headSha": HEAD, "totalFiles": 1, "truncated": False,
        "files": [{"filename": "src/app.ts", "status": "modified", "additions": 12,
                   "deletions": 4, "patch": "@@ -1 +1 @@", "previousFilename": None}],
    }
    assert captured[0]["url"] == f"https://api.github.com/repos/acme/widgets/compare/{BASE}...{HEAD}"


def test_github_single_commit_uses_first_parent_as_base(client, captured) -> None:
    captured.queue.append(_FakeResponse({"parents": [{"sha": BASE}], "files": []}))
    body = _post(client).json()
    assert body["baseSha"] == BASE
    assert captured[0]["url"] == f"https://api.github.com/repos/acme/widgets/commits/{HEAD}"


def test_github_token_becomes_a_bearer_header(client, captured) -> None:
    captured.queue.append(_FakeResponse({"files": []}))
    _post(client, gitToken="ghp_secret")
    assert captured[0]["headers"]["Authorization"] == "Bearer ghp_secret"


# ── GitLab: patch yes, counts NULL ────────────────────────────────────────────

def test_gitlab_reports_null_counts_not_zero(client, captured) -> None:
    captured.queue.append(_FakeResponse({"diffs": [
        {"new_path": "a.py", "diff": "@@ -1 +1 @@", "new_file": True},
        {"old_path": "gone.py", "deleted_file": True, "diff": "@@"},
    ]}))
    body = _post(client, repoUrl="https://gitlab.com/grp/sub/proj",
                 baseCommitId=BASE, gitToken="glpat").json()
    assert [f["status"] for f in body["files"]] == ["added", "removed"]
    # The whole point of the deviation: unknown != zero.
    assert all(f["additions"] is None and f["deletions"] is None for f in body["files"])
    assert body["files"][0]["patch"] == "@@ -1 +1 @@"
    assert captured[0]["headers"]["PRIVATE-TOKEN"] == "glpat"


def test_gitlab_nested_group_is_url_encoded_into_the_project_id(client, captured) -> None:
    captured.queue.append(_FakeResponse({"diffs": []}))
    _post(client, repoUrl="https://gitlab.com/grp/sub/proj", baseCommitId=BASE)
    assert captured[0]["url"].startswith("https://gitlab.com/api/v4/projects/grp%2Fsub%2Fproj/")


# ── Azure: counts and patch both NULL; blank-username Basic auth ──────────────

def test_azure_compare_returns_null_counts_and_keeps_leading_slash(client, captured) -> None:
    captured.queue.append(_FakeResponse({"changes": [
        {"item": {"path": "/src/Program.cs"}, "changeType": "edit"},
    ]}))
    body = _post(client, repoUrl="https://dev.azure.com/org/proj/_git/repo",
                 baseCommitId=BASE, gitToken="pat").json()
    f = body["files"][0]
    assert f == {"filename": "/src/Program.cs", "status": "edit", "additions": None,
                 "deletions": None, "patch": None, "previousFilename": None}
    # Azure PAT auth is Basic with a BLANK username.
    import base64
    assert captured[0]["headers"]["Authorization"] == "Basic " + base64.b64encode(b":pat").decode()


def test_azure_without_base_lists_the_tree_and_drops_the_byte_count(client, captured) -> None:
    """The backend put `item.size` (BYTES) in `additions` here. It is now null."""
    captured.queue.append(_FakeResponse({"value": [
        {"path": "/src/a.cs", "isFolder": False, "size": 4096},
        {"path": "/src", "isFolder": True},
    ]}))
    body = _post(client, repoUrl="https://dev.azure.com/org/proj/_git/repo").json()
    assert body["baseSha"] == ""
    assert body["files"] == [{"filename": "src/a.cs", "status": "added", "additions": None,
                              "deletions": None, "patch": None, "previousFilename": None}]


# ── Bitbucket: raw-diff parsing and the head..base spec order ─────────────────

def test_bitbucket_single_commit_parses_raw_diff_text(client, captured) -> None:
    raw = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1,2 +1,3 @@\n+added one\n+added two\n-removed one\n context\n"
    )
    captured.queue.append(_FakeResponse(raw))
    body = _post(client, repoUrl="https://bitbucket.org/team/repo", gitToken="user:key").json()
    assert body["files"] == [{"filename": "src/a.py", "status": "modified", "additions": 2,
                              "deletions": 1, "patch": raw.split("diff --git ", 1)[1],
                              "previousFilename": None}]


def test_bitbucket_compare_uses_head_dotdot_base(client, captured) -> None:
    captured.queue.append(_FakeResponse({"values": [
        {"new": {"path": "a.py"}, "status": "modified", "lines_added": 3, "lines_removed": 1},
    ]}))
    body = _post(client, repoUrl="https://bitbucket.org/team/repo",
                 baseCommitId=BASE, gitToken="user:key").json()
    assert captured[0]["url"].endswith(f"/diffstat/{HEAD}..{BASE}")
    # diffstat gives real counts but no patch text.
    assert body["files"][0]["additions"] == 3
    assert body["files"][0]["patch"] is None


def test_bitbucket_credential_without_colon_is_a_clean_400(client, captured) -> None:
    resp = _post(client, repoUrl="https://bitbucket.org/team/repo", gitToken="no-colon")
    assert resp.status_code == 400
    assert "username:api_key" in resp.json()["error"]


# ── Error contract ────────────────────────────────────────────────────────────

def test_unknown_host_is_a_400_not_a_silent_github_fallthrough(client, captured) -> None:
    """The backend mapped ANY unrecognised host to api.github.com. That is the bug
    this endpoint fixes: it errors instead of querying the wrong provider."""
    resp = _post(client, repoUrl="https://git.internal.corp/team/repo")
    assert resp.status_code == 400
    assert "supported hosts" in resp.json()["error"]
    assert captured == []  # nothing was ever requested


def test_provider_failure_surfaces_as_502(client, captured) -> None:
    captured.queue.append(_FakeResponse({"message": "Bad credentials"}, status_code=401))
    resp = _post(client, baseCommitId=BASE)
    assert resp.status_code == 502
    assert "401" in resp.json()["error"]


def test_missing_required_fields_is_a_400(client) -> None:
    assert client.post("/api/git-diff", json={"repoUrl": "https://github.com/a/b"}).status_code == 400
    assert client.post("/api/git-diff", json={"incomingCommitId": HEAD}).status_code == 400


@pytest.mark.parametrize("sentinel", [None, "", "null", "undefined"])
def test_base_sentinels_all_mean_single_commit(client, captured, sentinel) -> None:
    captured.queue.append(_FakeResponse({"parents": [], "files": []}))
    _post(client, baseCommitId=sentinel)
    assert "/compare/" not in captured[0]["url"]


def test_unused_fields_are_accepted_for_payload_symmetry(client, captured) -> None:
    captured.queue.append(_FakeResponse({"files": []}))
    resp = _post(client, baseCommitId=BASE, gitBranch="main",
                 projectUuid="37cb793f-1c61-4c6e-a0eb-85ff2631488e", codeOntologyId=42)
    assert resp.status_code == 200


# ── Rename tracking + truncation (added for sdlc's FileChange contract) ───────

def test_rename_reports_the_previous_filename(client, captured) -> None:
    """sdlc's `FileChange.previous_filename` needs this to follow a file across a
    move; without it a rename looks like an unrelated add plus delete."""
    captured.queue.append(_FakeResponse({"files": [
        {"filename": "new.ts", "status": "renamed", "previous_filename": "old.ts"},
    ]}))
    assert _post(client, baseCommitId=BASE).json()["files"][0]["previousFilename"] == "old.ts"


def test_gitlab_rename_reports_old_path(client, captured) -> None:
    captured.queue.append(_FakeResponse({"diffs": [
        {"new_path": "new.py", "old_path": "old.py", "renamed_file": True},
    ]}))
    body = _post(client, repoUrl="https://gitlab.com/grp/proj", baseCommitId=BASE).json()
    assert body["files"][0]["previousFilename"] == "old.py"
    assert body["files"][0]["status"] == "renamed"


def test_diff_envelope_always_carries_truncated(client, captured) -> None:
    """sdlc's meta-tests require a paginator to ANNOUNCE truncation, not just cap."""
    captured.queue.append(_FakeResponse({"files": []}))
    assert _post(client, baseCommitId=BASE).json()["truncated"] is False
