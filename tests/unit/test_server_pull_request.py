"""POST /api/pull-request — the centralised PR lookup (BREEZEAI-1228 follow-up).

Covers the per-provider mapping ported from BreezeAI_Backend's `GitService`, the
two pagination styles, the `includeCommits=false` path that replaces
`getPullRequestBaseBranch`, and the Azure base/head correction.
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

    def fake_get(url: str, headers=None, params=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "params": params or {}})
        return calls.queue.pop(0) if calls.queue else _FakeResponse({})

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _post(client: TestClient, **body) -> Any:
    payload = {"repoUrl": GH, "pullRequestId": 42}
    payload.update(body)
    return client.post("/api/pull-request", json=payload)


# ── GitHub ────────────────────────────────────────────────────────────────────

def test_github_maps_pr_and_pages_commits(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "number": 42, "title": "Add widget", "state": "open",
        "base": {"ref": "main", "sha": "BASE"}, "head": {"ref": "feat/x", "sha": "HEAD"},
    }))
    # A full page then a short page: the loop must stop after the second.
    captured.queue.append(_FakeResponse([
        {"sha": f"c{i}", "commit": {"message": "m", "author": {"name": "a", "date": "d"}}}
        for i in range(100)
    ]))
    captured.queue.append(_FakeResponse([
        {"sha": "tail", "commit": {"message": "m2", "author": {"name": "b", "date": "d2"}}}
    ]))
    body = _post(client).json()
    assert body["id"] == 42 and body["state"] == "open"
    assert body["baseBranch"] == "main" and body["sourceBranch"] == "feat/x"
    assert body["baseCommitSha"] == "BASE" and body["headCommitSha"] == "HEAD"
    assert len(body["commits"]) == 101
    assert body["commits"][-1] == {"sha": "tail", "message": "m2", "author": "b", "date": "d2"}
    assert [c["params"].get("page") for c in captured[1:]] == [1, 2]


def test_github_stops_paging_on_an_empty_page(client, captured) -> None:
    captured.queue.append(_FakeResponse({"base": {}, "head": {}}))
    captured.queue.append(_FakeResponse([]))
    assert _post(client).json()["commits"] == []
    assert len(captured) == 2  # PR + one commits page, no more


# ── includeCommits=false — the getPullRequestBaseBranch replacement ───────────

def test_include_commits_false_skips_the_commits_request(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "number": 7, "base": {"ref": "develop", "sha": "B"}, "head": {"ref": "f", "sha": "H"},
    }))
    body = _post(client, includeCommits=False).json()
    assert body["baseBranch"] == "develop"
    assert body["commits"] == []
    assert len(captured) == 1  # ONLY the PR fetch — this is what made it cheap


def test_include_commits_defaults_to_true(client, captured) -> None:
    captured.queue.append(_FakeResponse({"base": {}, "head": {}}))
    captured.queue.append(_FakeResponse([]))
    _post(client)
    assert len(captured) == 2


# ── GitLab ────────────────────────────────────────────────────────────────────

def test_gitlab_maps_merge_request_vocabulary(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "iid": 9, "title": "MR", "state": "opened",
        "target_branch": "main", "source_branch": "feat",
        "diff_refs": {"base_sha": "B", "head_sha": "H2"}, "sha": "H",
    }))
    body = _post(client, repoUrl=GL, includeCommits=False).json()
    assert body["id"] == 9 and body["baseBranch"] == "main"
    assert body["baseCommitSha"] == "B"
    assert body["headCommitSha"] == "H"  # `sha` wins over diff_refs.head_sha


def test_gitlab_without_diff_refs_yields_empty_not_a_branch_name(client, captured) -> None:
    """FIXED: this used to fall back to `target_branch` — a branch NAME, which the
    diff API would resolve to wherever that branch points NOW, silently folding in
    unrelated commits. "" lets the caller's base/head guard fire instead."""
    captured.queue.append(_FakeResponse({"iid": 9, "target_branch": "main", "sha": "H"}))
    body = _post(client, repoUrl=GL, includeCommits=False).json()
    assert body["baseCommitSha"] == ""
    assert body["baseBranch"] == "main"  # still reported, just not as a SHA
    assert body["headCommitSha"] == "H"  # unaffected: both its sources are real SHAs


# ── Azure: the base/head correction and the unpaginated commits ───────────────

def test_azure_base_is_target_and_head_is_source(client, captured) -> None:
    """The backend had these swapped, reversing every Azure PR diff."""
    captured.queue.append(_FakeResponse({
        "pullRequestId": 5, "title": "PR", "status": "active",
        "targetRefName": "refs/heads/main", "sourceRefName": "refs/heads/feature/x",
        "lastMergeTargetCommit": {"commitId": "TARGET"},
        "lastMergeSourceCommit": {"commitId": "SOURCE"},
    }))
    body = _post(client, repoUrl=AZ, includeCommits=False).json()
    assert body["baseCommitSha"] == "TARGET"   # target == base
    assert body["headCommitSha"] == "SOURCE"   # source == head
    assert body["baseBranch"] == "main" and body["sourceBranch"] == "feature/x"


def test_azure_missing_merge_commits_yield_empty_not_a_ref_name(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "pullRequestId": 5, "targetRefName": "refs/heads/main", "sourceRefName": "refs/heads/f",
    }))
    body = _post(client, repoUrl=AZ, includeCommits=False).json()
    assert body["baseCommitSha"] == "" and body["headCommitSha"] == ""


def test_azure_follows_the_continuation_token_header(client, captured) -> None:
    """FIXED: Azure used to fetch one page only, truncating long PRs in silence.
    It signals "more" via a response HEADER, not a body field."""
    captured.queue.append(_FakeResponse({"pullRequestId": 5}))
    captured.queue.append(_FakeResponse(
        {"value": [{"commitId": "c1", "comment": "m1", "author": {"name": "a", "date": "d"}}]},
        headers={"x-ms-continuationtoken": "TOKEN2"}))
    captured.queue.append(_FakeResponse(
        {"value": [{"commitId": "c2", "comment": "m2", "author": {"name": "b", "date": "e"}}]}))
    body = _post(client, repoUrl=AZ).json()
    assert [c["sha"] for c in body["commits"]] == ["c1", "c2"]
    assert captured[1]["params"]["$top"] == 100
    assert "continuationToken" not in captured[1]["params"]      # first page: none
    assert captured[2]["params"]["continuationToken"] == "TOKEN2"  # second page: echoed back


def test_azure_stops_when_no_continuation_token(client, captured) -> None:
    captured.queue.append(_FakeResponse({"pullRequestId": 5}))
    captured.queue.append(_FakeResponse({"value": [
        {"commitId": "c1", "comment": "m", "author": {"name": "a", "date": "d"}},
    ]}))
    body = _post(client, repoUrl=AZ).json()
    assert len(body["commits"]) == 1
    assert len(captured) == 2  # PR + one commits page, then stop


def test_azure_stops_on_an_empty_page_even_with_a_token(client, captured) -> None:
    """A token plus no values would otherwise loop forever."""
    captured.queue.append(_FakeResponse({"pullRequestId": 5}))
    captured.queue.append(_FakeResponse({"value": []}, headers={"x-ms-continuationtoken": "T"}))
    assert _post(client, repoUrl=AZ).json()["commits"] == []
    assert len(captured) == 2


# ── Bitbucket: cursor pagination ──────────────────────────────────────────────

def test_bitbucket_follows_the_next_cursor(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "id": 3, "title": "PR", "state": "OPEN",
        "destination": {"branch": {"name": "main"}, "commit": {"hash": "B"}},
        "source": {"branch": {"name": "feat"}, "commit": {"hash": "H"}},
    }))
    captured.queue.append(_FakeResponse({
        "values": [{"hash": "c1", "message": "m", "author": {"raw": "a"}, "date": "d"}],
        "next": "https://api.bitbucket.org/page2",
    }))
    captured.queue.append(_FakeResponse({
        "values": [{"hash": "c2", "message": "m2",
                    "author": {"user": {"display_name": "b"}}, "date": "d2"}],
    }))
    body = _post(client, repoUrl=BB, gitToken="user:key").json()
    assert body["baseBranch"] == "main" and body["baseCommitSha"] == "B"
    assert [c["sha"] for c in body["commits"]] == ["c1", "c2"]
    assert body["commits"][1]["author"] == "b"  # display_name fallback
    assert captured[2]["url"] == "https://api.bitbucket.org/page2"


# ── Error contract ────────────────────────────────────────────────────────────

def test_unknown_host_is_a_400(client, captured) -> None:
    resp = _post(client, repoUrl="https://git.internal.corp/t/r")
    assert resp.status_code == 400 and captured == []


def test_provider_failure_surfaces_as_502(client, captured) -> None:
    captured.queue.append(_FakeResponse({"message": "Not Found"}, status_code=404))
    resp = _post(client)
    assert resp.status_code == 502
    assert "404" in resp.json()["error"]


def test_missing_required_fields_is_a_400(client) -> None:
    assert client.post("/api/pull-request", json={"repoUrl": GH}).status_code == 400
    assert client.post("/api/pull-request", json={"pullRequestId": 1}).status_code == 400


# ── _azure_paged internals ────────────────────────────────────────────────────

def test_azure_paged_safety_stop_prevents_an_infinite_loop(monkeypatch) -> None:
    """A provider that echoes the same token forever must not hang the request."""
    import httpx

    from breezeai_cog.server import git as git_mod

    calls = {"n": 0}

    def always_more(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse({"value": [{"commitId": f"c{calls['n']}"}]},
                             headers={"x-ms-continuationtoken": "SAME"})

    monkeypatch.setattr(httpx, "get", always_more)
    out = git_mod._azure_paged("u", {}, {}, lambda c: c, page_size=10, max_pages=5)
    assert calls["n"] == 5      # stopped at the cap, did not spin
    assert len(out) == 5        # and kept what it had rather than failing
