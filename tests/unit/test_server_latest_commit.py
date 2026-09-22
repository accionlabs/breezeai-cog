"""POST /api/latest-commit and POST /api/pr-comment (BREEZEAI-1228 follow-up 2).

The last two provider-touching methods moved out of BreezeAI_Backend's
`GitService`. Covers the per-provider mapping, GitHub's and Azure's two-hop
lookups, and the Azure comment fix.
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


class _FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
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
            calls.append({"method": method, "url": url, "headers": headers or {},
                          "params": params or {}, "json": json})
            return calls.queue.pop(0) if calls.queue else _FakeResponse({})
        return fake

    monkeypatch.setattr(httpx, "get", record("GET"))
    monkeypatch.setattr(httpx, "post", record("POST"))
    return calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


# ── latest-commit ─────────────────────────────────────────────────────────────

def test_github_takes_two_hops_for_message_and_author(client, captured) -> None:
    """The branch payload carries only a commit stub, hence the second request."""
    captured.queue.append(_FakeResponse({"commit": {"sha": "SHA1"}}))
    captured.queue.append(_FakeResponse(
        {"commit": {"message": "fix things", "author": {"name": "Ada", "date": "2026-01-01"}}}))
    body = client.post("/api/latest-commit", json={"repoUrl": GH, "gitBranch": "main"}).json()
    assert body == {"sha": "SHA1", "message": "fix things", "author": "Ada", "date": "2026-01-01"}
    assert captured[1]["url"].endswith("/commits/SHA1")


def test_gitlab_reads_it_all_from_one_branch_call(client, captured) -> None:
    captured.queue.append(_FakeResponse({"commit": {
        "id": "SHA2", "message": "m", "author_name": "Bob", "committed_date": "2026-01-02"}}))
    body = client.post("/api/latest-commit", json={"repoUrl": GL, "gitToken": "glpat"}).json()
    assert body == {"sha": "SHA2", "message": "m", "author": "Bob", "date": "2026-01-02"}
    assert len(captured) == 1


def test_bitbucket_falls_back_to_display_name(client, captured) -> None:
    captured.queue.append(_FakeResponse({"target": {
        "hash": "SHA3", "message": "m", "date": "d", "author": {"user": {"display_name": "Cy"}}}}))
    body = client.post("/api/latest-commit", json={"repoUrl": BB, "gitToken": "u:k"}).json()
    assert body["sha"] == "SHA3" and body["author"] == "Cy"


def test_azure_resolves_the_ref_then_the_commit(client, captured) -> None:
    captured.queue.append(_FakeResponse({"value": [{"objectId": "SHA4"}]}))
    captured.queue.append(_FakeResponse({"comment": "msg", "author": {"name": "Dee", "date": "d"}}))
    body = client.post("/api/latest-commit", json={"repoUrl": AZ, "gitToken": "pat"}).json()
    assert body == {"sha": "SHA4", "message": "msg", "author": "Dee", "date": "d"}
    assert captured[0]["params"]["filter"] == "heads/main"


def test_azure_missing_branch_is_a_404_not_a_500(client, captured) -> None:
    captured.queue.append(_FakeResponse({"value": []}))
    resp = client.post("/api/latest-commit", json={"repoUrl": AZ, "gitBranch": "nope"})
    assert resp.status_code == 404
    assert "nope" in resp.json()["error"]


def test_branch_defaults_to_main(client, captured) -> None:
    captured.queue.append(_FakeResponse({"commit": {"sha": "S"}}))
    captured.queue.append(_FakeResponse({"commit": {"author": {}}}))
    client.post("/api/latest-commit", json={"repoUrl": GH})
    assert captured[0]["url"].endswith("/branches/main")


def test_latest_commit_requires_repo_url(client) -> None:
    assert client.post("/api/latest-commit", json={}).status_code == 400


# ── pr-comment ────────────────────────────────────────────────────────────────

def test_github_comments_go_to_the_issues_endpoint(client, captured) -> None:
    captured.queue.append(_FakeResponse({"id": 1}))
    body = client.post("/api/pr-comment", json={
        "repoUrl": GH, "pullRequestId": 42, "body": "hello", "gitToken": "t"}).json()
    assert body == {"posted": True, "provider": "github"}
    assert captured[0]["method"] == "POST"
    assert captured[0]["url"].endswith("/issues/42/comments")
    assert captured[0]["json"] == {"body": "hello"}


def test_gitlab_posts_a_merge_request_note(client, captured) -> None:
    captured.queue.append(_FakeResponse({}))
    client.post("/api/pr-comment", json={
        "repoUrl": GL, "pullRequestId": 9, "body": "hi", "gitToken": "t"})
    assert captured[0]["url"].endswith("/merge_requests/9/notes")
    assert captured[0]["json"] == {"body": "hi"}


def test_bitbucket_wraps_the_body_in_content_raw(client, captured) -> None:
    captured.queue.append(_FakeResponse({}))
    client.post("/api/pr-comment", json={
        "repoUrl": BB, "pullRequestId": 3, "body": "hi", "gitToken": "u:k"})
    assert captured[0]["url"].endswith("/pullrequests/3/comments")
    assert captured[0]["json"] == {"content": {"raw": "hi"}}


def test_azure_uses_the_threads_api_not_a_github_shaped_body(client, captured) -> None:
    """The backend sent {body} to the PR resource URL; Azure rejected it and the
    caller swallowed the error, so skip-comments silently never appeared."""
    captured.queue.append(_FakeResponse({}))
    client.post("/api/pr-comment", json={
        "repoUrl": AZ, "pullRequestId": 5, "body": "hi", "gitToken": "pat"})
    assert captured[0]["url"].endswith("/pullRequests/5/threads")
    assert captured[0]["params"]["api-version"] == "7.1"
    assert captured[0]["json"] == {
        "comments": [{"parentCommentId": 0, "content": "hi", "commentType": 1}], "status": 1}


def test_provider_rejection_surfaces_as_502(client, captured) -> None:
    captured.queue.append(_FakeResponse({"message": "Forbidden"}, status_code=403))
    resp = client.post("/api/pr-comment", json={
        "repoUrl": GH, "pullRequestId": 1, "body": "x", "gitToken": "t"})
    assert resp.status_code == 502


def test_pr_comment_requires_all_three_fields(client) -> None:
    base = {"repoUrl": GH, "pullRequestId": 1, "body": "x"}
    for missing in ("repoUrl", "pullRequestId", "body"):
        payload = {k: v for k, v in base.items() if k != missing}
        assert client.post("/api/pr-comment", json=payload).status_code == 400, missing


def test_unknown_host_never_reaches_a_provider(client, captured) -> None:
    resp = client.post("/api/pr-comment", json={
        "repoUrl": "https://git.internal.corp/t/r", "pullRequestId": 1, "body": "x"})
    assert resp.status_code == 400 and captured == []
