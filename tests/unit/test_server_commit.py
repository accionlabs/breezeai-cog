"""POST /api/commit, /api/commit-comment, /api/commit-status (BREEZEAI-1228 f/u 4).

The three `AbstractSCMClient` methods COG did not cover, blocking
sdlc-autonomous-agents from retiring its own `scm/` layer. Covers the four
providers' differing vocabularies, description caps, and the two "not posted"
outcomes that are real results rather than errors.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from breezeai_cog.server.app import create_app

GH = "https://github.com/acme/widgets"
GL = "https://gitlab.com/grp/proj"
BB = "https://bitbucket.org/team/repo"
AZ = "https://dev.azure.com/org/proj/_git/repo"
SHA = "01d9ad8afd9e6c6dc35882e947d86d357f1994a7"


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200,
                 headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self) -> Any:
        return self._payload


class _Captured(list):
    def __init__(self) -> None:
        super().__init__()
        self.queue: list[_FakeResponse] = []


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _Captured:
    import httpx

    calls = _Captured()

    def record(method: str):
        def fake(url: str, headers=None, params=None, json=None, timeout=None):
            calls.append({"method": method, "url": url, "params": params or {}, "json": json})
            return calls.queue.pop(0) if calls.queue else _FakeResponse({})
        return fake

    monkeypatch.setattr(httpx, "get", record("GET"))
    monkeypatch.setattr(httpx, "post", record("POST"))
    return calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


# ── /api/commit ───────────────────────────────────────────────────────────────

def test_github_commit_maps_detail_and_files(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "sha": SHA, "html_url": "https://github.com/acme/widgets/commit/abc",
        "commit": {"message": "fix", "author": {"name": "Ada", "date": "2026-01-01"}},
        "files": [{"filename": "a.ts", "status": "renamed", "additions": 2, "deletions": 1,
                   "patch": "@@", "previous_filename": "old.ts"}],
    }))
    body = client.post("/api/commit", json={"repoUrl": GH, "sha": SHA}).json()
    assert body["sha"] == SHA and body["author"] == "Ada"
    assert body["files"][0]["previousFilename"] == "old.ts"
    assert body["truncated"] is False
    # jiraTicketKeys is the caller's job, not ours.
    assert "jiraTicketKeys" not in body


def test_github_commit_flags_the_300_file_cap(client, captured) -> None:
    """That endpoint cannot be paginated, so a full page means "incomplete"."""
    captured.queue.append(_FakeResponse({
        "sha": SHA, "commit": {"author": {}},
        "files": [{"filename": f"f{i}.ts", "status": "modified"} for i in range(300)],
    }))
    assert client.post("/api/commit", json={"repoUrl": GH, "sha": SHA}).json()["truncated"] is True


def test_gitlab_commit_pages_the_diff(client, captured) -> None:
    """GitLab's commit-diff endpoint pages at 20 by default — it MUST be paged."""
    captured.queue.append(_FakeResponse({"id": SHA, "message": "m", "author_name": "Bo",
                                         "committed_date": "d", "web_url": "u"}))
    captured.queue.append(_FakeResponse([{"new_path": f"f{i}.py"} for i in range(100)]))
    captured.queue.append(_FakeResponse([{"new_path": "tail.py", "renamed_file": True,
                                          "old_path": "was.py"}]))
    body = client.post("/api/commit", json={"repoUrl": GL, "sha": SHA}).json()
    assert len(body["files"]) == 101
    assert body["files"][-1]["previousFilename"] == "was.py"


def test_bitbucket_commit_joins_diffstat_counts_with_raw_patches(client, captured) -> None:
    captured.queue.append(_FakeResponse({"hash": SHA, "message": "m", "date": "d",
                                         "author": {"raw": "Cy"}}))
    captured.queue.append(_FakeResponse({"values": [
        {"new": {"path": "a.py"}, "status": "modified", "lines_added": 3, "lines_removed": 1}]}))
    captured.queue.append(_FakeResponse(
        "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n+new\n-old\n"))
    body = client.post("/api/commit", json={"repoUrl": BB, "sha": SHA, "gitToken": "u:k"}).json()
    f = body["files"][0]
    assert f["additions"] == 3 and f["deletions"] == 1   # from diffstat
    assert f["patch"] is not None                        # from the raw diff


def test_azure_commit_renders_patches_from_blobs(client, captured) -> None:
    """Azure exposes no diff endpoint, so patches are rendered locally."""
    captured.queue.append(_FakeResponse({"commitId": SHA, "comment": "m",
                                         "author": {"name": "Dee", "date": "d"}}))
    captured.queue.append(_FakeResponse({"value": [
        {"item": {"path": "/src/a.cs", "objectId": "AFTER"}, "changeType": "edit",
         "originalObjectId": "BEFORE"}]}))
    captured.queue.append(_FakeResponse("line one\n"))    # before blob
    captured.queue.append(_FakeResponse("line two\n"))    # after blob
    body = client.post("/api/commit", json={"repoUrl": AZ, "sha": SHA, "gitToken": "pat"}).json()
    f = body["files"][0]
    assert f["filename"] == "src/a.cs" and f["status"] == "modified"
    assert "+line two" in f["patch"] and "-line one" in f["patch"]
    assert f["additions"] == 1 and f["deletions"] == 1


def test_azure_binary_blob_yields_no_patch(client, captured) -> None:
    captured.queue.append(_FakeResponse({"commitId": SHA, "author": {}}))
    captured.queue.append(_FakeResponse({"value": [
        {"item": {"path": "/img.png", "objectId": "A"}, "changeType": "edit"}]}))
    captured.queue.append(_FakeResponse("\x00\x01binary"))
    body = client.post("/api/commit", json={"repoUrl": AZ, "sha": SHA}).json()
    assert body["files"][0]["patch"] is None


def test_commit_requires_repo_url_and_sha(client) -> None:
    assert client.post("/api/commit", json={"repoUrl": GH}).status_code == 400
    assert client.post("/api/commit", json={"sha": SHA}).status_code == 400


# ── /api/commit-comment ───────────────────────────────────────────────────────

@pytest.mark.parametrize("repo,path,body_key", [
    (GH, "/commits/{sha}/comments", {"body": "hi"}),
    (GL, "/repository/commits/{sha}/comments", {"note": "hi"}),   # GitLab says `note`
    (BB, "/commit/{sha}/comments", {"content": {"raw": "hi"}}),
])
def test_commit_comment_uses_each_providers_shape(client, captured, repo, path, body_key) -> None:
    captured.queue.append(_FakeResponse({}))
    resp = client.post("/api/commit-comment", json={
        "repoUrl": repo, "sha": SHA, "body": "hi", "gitToken": "u:k"})
    assert resp.json()["posted"] is True
    assert captured[0]["url"].endswith(path.format(sha=SHA))
    assert captured[0]["json"] == body_key


def test_azure_commit_comment_reports_not_posted_rather_than_failing(client, captured) -> None:
    """Azure has no commit-comment API. Callers must not report a comment that
    does not exist, so this is an explicit outcome, not an exception."""
    resp = client.post("/api/commit-comment", json={"repoUrl": AZ, "sha": SHA, "body": "hi"})
    assert resp.status_code == 200
    assert resp.json()["posted"] is False
    assert "no commit-comment API" in resp.json()["reason"]
    assert captured == []


# ── /api/commit-status ────────────────────────────────────────────────────────

@pytest.mark.parametrize("state,gh,gl,bb,az", [
    ("pending", "pending", "pending", "INPROGRESS", "pending"),
    ("success", "success", "success", "SUCCESSFUL", "succeeded"),
    ("failure", "failure", "failed", "FAILED", "failed"),
    ("error", "error", "failed", "FAILED", "error"),
])
def test_each_provider_gets_its_own_state_vocabulary(client, captured, state, gh, gl, bb, az) -> None:
    for repo, expected, extra in ((GH, gh, {}), (GL, gl, {}),
                                  (BB, bb, {"buildKey": "breeze"}), (AZ, az, {})):
        captured.clear()
        captured.queue.append(_FakeResponse({}))
        resp = client.post("/api/commit-status", json={
            "repoUrl": repo, "sha": SHA, "state": state, "description": "d",
            "context": "breezeai/scope", "targetUrl": "https://x", "gitToken": "u:k", **extra})
        assert resp.json()["state"] == expected, (repo, state)


def test_description_is_capped_per_provider(client, captured) -> None:
    long = "x" * 5000
    for repo, cap, extra in ((GH, 140, {}), (GL, 250, {}),
                             (BB, 255, {"buildKey": "k"}), (AZ, 4000, {})):
        captured.clear()
        captured.queue.append(_FakeResponse({}))
        client.post("/api/commit-status", json={
            "repoUrl": repo, "sha": SHA, "state": "success", "description": long,
            "gitToken": "u:k", **extra})
        assert len(captured[0]["json"]["description"]) == cap, repo


def test_bitbucket_without_a_build_key_is_not_posted(client, captured) -> None:
    resp = client.post("/api/commit-status", json={
        "repoUrl": BB, "sha": SHA, "state": "success", "gitToken": "u:k"})
    assert resp.status_code == 200 and resp.json()["posted"] is False
    assert "buildKey" in resp.json()["reason"]
    assert captured == []


def test_azure_splits_context_into_genre_and_name(client, captured) -> None:
    captured.queue.append(_FakeResponse({}))
    client.post("/api/commit-status", json={
        "repoUrl": AZ, "sha": SHA, "state": "success", "context": "breezeai/scope"})
    assert captured[0]["json"]["context"] == {"genre": "breezeai", "name": "scope"}


def test_unknown_state_is_coerced_to_pending(client, captured) -> None:
    captured.queue.append(_FakeResponse({}))
    resp = client.post("/api/commit-status", json={
        "repoUrl": GH, "sha": SHA, "state": "banana"})
    assert resp.json()["state"] == "pending"


def test_commit_status_requires_state(client) -> None:
    assert client.post("/api/commit-status",
                       json={"repoUrl": GH, "sha": SHA}).status_code == 400


# ── Azure API version is pinned once ──────────────────────────────────────────

def test_every_azure_call_names_the_same_api_version(client, captured) -> None:
    """Azure versions its payload shapes, so an unversioned or drifting call can
    change shape under us with no code change. This caught a real drift: the
    backend was on 6.0 while sdlc-autonomous-agents was on 7.1."""
    from breezeai_cog.server import git as git_mod

    captured.queue.append(_FakeResponse({"commitId": SHA, "author": {}}))
    captured.queue.append(_FakeResponse({"changes": [
        {"item": {"path": "/a.cs", "objectId": "A"}, "changeType": "edit"},
    ]}))
    captured.queue.append(_FakeResponse(""))
    captured.queue.append(_FakeResponse("x\n"))
    client.post("/api/commit", json={"repoUrl": AZ, "sha": SHA, "gitToken": "pat"})

    versioned = [c for c in captured if "api-version" in c["params"]]
    assert versioned, "no Azure call named an api-version"
    assert {c["params"]["api-version"] for c in versioned} == {git_mod._ADO_API_VERSION}
    # Including the blob fetch, which previously carried no version at all.
    assert any("/blobs/" in c["url"] for c in versioned), (
        "the blob fetch is unversioned; Azure would apply a default that can "
        "shift without a code change"
    )


def test_the_version_is_pinned_in_exactly_one_place() -> None:
    """Nine copies is how the drift happened; one constant is the fix."""
    import pathlib

    src = pathlib.Path(
        "src/breezeai_cog/server/git.py"
    ).read_text()
    assert '"6.0"' not in src, "a hardcoded Azure api-version survived the bump"
    assert src.count('_ADO_API_VERSION = ') == 1
