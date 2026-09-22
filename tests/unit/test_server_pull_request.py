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
    commit_pages = [c for c in captured if c["url"].endswith("/commits")]
    assert [c["params"].get("page") for c in commit_pages] == [1, 2]
    assert captured[-1]["url"].endswith("/reviews")  # approvals, fetched last


def test_github_stops_paging_on_an_empty_page(client, captured) -> None:
    captured.queue.append(_FakeResponse({"base": {}, "head": {}}))
    captured.queue.append(_FakeResponse([]))
    captured.queue.append(_FakeResponse([]))  # reviews
    assert _post(client).json()["commits"] == []
    assert [c["url"].rsplit("/", 1)[-1] for c in captured] == ["42", "commits", "reviews"]


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
    captured.queue.append(_FakeResponse([]))  # reviews
    _post(client)
    assert len(captured) == 3  # PR + commits + reviews


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


# ── PRDetails enrichment for Merge Validation ─────────────────────────────────

def test_github_reports_approvals_labels_and_reviewers(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "number": 1, "state": "closed", "merged_at": "2026-01-02", "body": "desc",
        "user": {"login": "ada"}, "html_url": "u", "created_at": "2026-01-01",
        "labels": [{"name": "bug"}], "requested_reviewers": [{"login": "bo"}],
        "base": {"ref": "main", "sha": "B"}, "head": {"ref": "f", "sha": "H"},
    }))
    captured.queue.append(_FakeResponse([]))  # commits
    captured.queue.append(_FakeResponse([
        {"state": "APPROVED", "user": {"login": "cy"}},
        {"state": "COMMENTED", "user": {"login": "dee"}},
    ]))
    body = _post(client).json()
    assert body["state"] == "merged"          # closed + merged_at
    assert body["approvals"] == ["cy"]        # COMMENTED is not an approval
    assert body["labels"] == ["bug"] and body["reviewers"] == ["bo"]
    assert body["author"] == "ada" and body["description"] == "desc"


def test_github_review_failure_degrades_to_empty_approvals(client, captured) -> None:
    """A reviews outage must not fail the whole PR fetch."""
    captured.queue.append(_FakeResponse({"base": {}, "head": {}}))
    captured.queue.append(_FakeResponse([]))
    captured.queue.append(_FakeResponse({"message": "boom"}, status_code=500))
    assert _post(client).json()["approvals"] == []


def test_lightweight_fetch_skips_the_approvals_round_trip(client, captured) -> None:
    """`includeCommits=false` exists to avoid extra calls — approvals cost one."""
    captured.queue.append(_FakeResponse({"base": {"ref": "main"}, "head": {}}))
    body = _post(client, includeCommits=False).json()
    assert body["approvals"] == []   # "not fetched", not "nobody approved"
    assert len(captured) == 1


def test_azure_approval_is_vote_ten(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "pullRequestId": 5, "status": "completed",
        "reviewers": [{"displayName": "Ada", "vote": 10}, {"displayName": "Bo", "vote": -5}],
    }))
    body = _post(client, repoUrl=AZ, includeCommits=False).json()
    assert body["approvals"] == ["Ada"]
    assert body["reviewers"] == ["Ada", "Bo"]
    assert body["state"] == "merged"   # Azure says "completed"


def test_bitbucket_approval_comes_from_participants(client, captured) -> None:
    captured.queue.append(_FakeResponse({
        "id": 3, "state": "MERGED",
        "participants": [{"approved": True, "user": {"display_name": "Ada"}},
                         {"approved": False, "user": {"display_name": "Bo"}}],
        "destination": {"branch": {"name": "main"}}, "source": {"branch": {"name": "f"}},
    }))
    body = _post(client, repoUrl=BB, gitToken="u:k", includeCommits=False).json()
    assert body["approvals"] == ["Ada"]
    assert body["state"] == "merged"
    assert body["labels"] == []   # Bitbucket Cloud has no PR labels


# ── Azure page envelopes ──────────────────────────────────────────────────────

@pytest.mark.parametrize(("body", "expected"), [
    ({"value": [1, 2]}, [1, 2]),
    ({"changes": [1, 2, 3]}, [1, 2, 3]),        # commit changes
    ({"changeEntries": [1]}, [1]),              # PR iteration changes
    ([1, 2], [1, 2]),
    ({}, []),
    ({"unexpected": "shape"}, []),
    (None, []),
])
def test_azure_page_items_reads_every_envelope(body, expected) -> None:
    """Azure wraps change endpoints differently from everything else. Reading
    only `value` yields an empty list — no error, no files, and a caller that
    validates nothing and passes vacuously."""
    from breezeai_cog.server import git as git_mod

    assert git_mod._azure_page_items(body) == expected


def test_azure_commit_changes_use_the_changes_envelope(client, captured) -> None:
    """Regression: `/commits/{sha}/changes` returns {"changes": [...]}, so a
    value-only reader reports a commit with no files changed."""
    captured.queue.append(_FakeResponse({"commitId": "abc", "author": {}}))
    captured.queue.append(_FakeResponse({"changes": [
        {"item": {"path": "/a.cs", "objectId": "A"}, "changeType": "add"},
    ]}))
    captured.queue.append(_FakeResponse(""))       # before blob (add → empty)
    captured.queue.append(_FakeResponse("new\n"))  # after blob
    body = client.post("/api/commit", json={"repoUrl": AZ, "sha": "abc"}).json()
    assert [f["filename"] for f in body["files"]] == ["a.cs"]
