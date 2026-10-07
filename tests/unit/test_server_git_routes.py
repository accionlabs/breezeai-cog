"""`/api/git/*` — the provider operations exposed for the backend's check-update flow.
The provider client is faked through `ServerDeps.open_scm`; no network."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from breezeai_cog.config import Settings
from breezeai_cog.integrations.scm import (
    AbstractSCMClient,
    ChangeSet,
    CommitInfo,
    PrComment,
    PullRequestInfo,
    RepoRef,
    SCMAPIError,
    SCMCredentialError,
)
from breezeai_cog.server.app import create_app
from breezeai_cog.server.deps import ServerDeps
from breezeai_cog.server.git import INVALID_REPO_URL_MESSAGE

SHA = "a" * 40
OLD = "b" * 40
REPO = "https://github.com/acme/widgets"


class FakeClient(AbstractSCMClient):
    provider = "github"
    supports_incremental = True

    def __init__(self, *, tip=None, changed=(), deleted=(), tree=(), fail=None, pr=None) -> None:
        self._tip = tip or CommitInfo(SHA, "feat: x", "Alice", "2026-09-01T00:00:00Z")
        self._pr = pr or PullRequestInfo(7, "Add x", "open", "main", "feat/x", OLD, SHA, "https://github.com/acme/widgets/pull/7")
        self._changes = ChangeSet(list(changed), list(deleted))
        self._tree = list(tree)
        self._fail = fail
        self.calls: list[tuple] = []
        self.closed = False

    def _maybe_fail(self):
        if self._fail:
            raise self._fail

    def branch_head(self, ref, branch):
        self.calls.append(("branch_head", branch))
        self._maybe_fail()
        return self._tip

    def compare(self, ref, base, head):
        self.calls.append(("compare", base, head))
        self._maybe_fail()
        return self._changes

    def tree(self, ref, commit):
        self.calls.append(("tree", commit))
        self._maybe_fail()
        return self._tree

    def pull_request(self, ref, number):
        self.calls.append(("pull_request", number))
        self._maybe_fail()
        return self._pr

    def post_pr_comment(self, ref, number, body):
        self.calls.append(("post_pr_comment", number, body))
        self._maybe_fail()
        return PrComment("123", "https://github.com/acme/widgets/pull/7#issuecomment-123")

    def file_content(self, ref, path, commit):
        raise NotImplementedError

    def clone_url(self, ref):
        return "https://github.com/acme/widgets.git"

    def close(self):
        self.closed = True


def _app(client: FakeClient, settings: Settings | None = None, seen: list | None = None) -> TestClient:
    settings = settings or Settings(_env_file=None)

    def open_scm(ref: RepoRef, s: Settings, token: str | None) -> AbstractSCMClient:
        if seen is not None:
            seen.append((ref, token))
        return client

    deps = ServerDeps(
        settings=settings,
        open_storage=lambda key: (_ for _ in ()).throw(AssertionError("no storage here")),
        notify=lambda path, payload: None,
        open_scm=open_scm,
    )
    return TestClient(create_app(settings, deps))


# --- check-update -------------------------------------------------------------------


def test_check_update_behind_runs_compare_and_reports_counts() -> None:
    fake = FakeClient(changed=["a.py", "b.py"], deleted=["gone.py"])
    seen: list = []
    r = _app(fake, seen=seen).post("/api/git/check-update", json={
        "repoUrl": REPO, "gitBranch": "main", "gitToken": "tok", "currentCommitId": OLD,
    })
    assert r.status_code == 200
    assert r.json() == {
        "change": True,
        "latestCommitId": SHA,
        "currentCommitId": OLD,
        "fileUpdatedCount": 2,
        "fileDeletedCount": 1,
        "latestCommit": {"sha": SHA, "message": "feat: x", "author": "Alice", "date": "2026-09-01T00:00:00Z"},
        "changedFiles": ["a.py", "b.py"],
        "deletedFiles": ["gone.py"],
    }
    assert fake.calls == [("branch_head", "main"), ("compare", OLD, SHA)]
    assert seen == [(RepoRef("github", "acme", "widgets", host="github.com"), "tok")]
    assert fake.closed


def test_check_update_up_to_date_skips_compare() -> None:
    fake = FakeClient(changed=["should-not-be-read.py"])
    r = _app(fake).post("/api/git/check-update", json={
        "repoUrl": REPO, "gitBranch": "main", "currentCommitId": SHA,
    })
    body = r.json()
    assert r.status_code == 200
    assert body["change"] is False and body["latestCommitId"] == SHA
    assert body["fileUpdatedCount"] == 0 and body["changedFiles"] == []
    assert fake.calls == [("branch_head", "main")]


@pytest.mark.parametrize("current", [None, "", "null", "undefined"])
def test_check_update_without_stored_commit_is_change_with_no_diff(current) -> None:
    fake = FakeClient(changed=["a.py"])
    payload = {"repoUrl": REPO, "gitBranch": "main"}
    if current is not None:
        payload["currentCommitId"] = current
    body = _app(fake).post("/api/git/check-update", json=payload).json()
    assert body["change"] is True and body["currentCommitId"] is None
    assert body["fileUpdatedCount"] == 0 and body["latestCommitId"] == SHA
    assert fake.calls == [("branch_head", "main")]


def test_check_update_requires_repo_and_branch() -> None:
    r = _app(FakeClient()).post("/api/git/check-update", json={"repoUrl": REPO})
    assert r.status_code == 400 and r.json() == {"error": "All fields required: repoUrl, gitBranch"}


def test_invalid_repo_url_is_400() -> None:
    r = _app(FakeClient()).post("/api/git/check-update", json={"repoUrl": "https://example.com/x/y", "gitBranch": "main"})
    assert r.status_code == 400 and r.json() == {"error": INVALID_REPO_URL_MESSAGE}


def test_self_hosted_url_accepted_when_configured() -> None:
    seen: list = []
    settings = Settings(_env_file=None, scm_instances={"git.acme.com": "gitlab"})
    r = _app(FakeClient(), settings, seen).post("/api/git/check-update", json={
        "repoUrl": "https://git.acme.com/grp/proj", "gitBranch": "main",
    })
    assert r.status_code == 200
    assert seen[0][0] == RepoRef("gitlab", "grp", "proj", host="git.acme.com")


def test_provider_failure_is_502_with_hint_and_no_body() -> None:
    fake = FakeClient(fail=SCMAPIError(
        "GitHub API GET /repos/acme/widgets/branches/main failed with HTTP 401: the credential was rejected; check the token is valid for this host",
        http_status=401,
    ))
    r = _app(fake).post("/api/git/check-update", json={"repoUrl": REPO, "gitBranch": "main"})
    assert r.status_code == 502
    assert "HTTP 401" in r.json()["error"] and "credential was rejected" in r.json()["error"]
    assert fake.closed


def test_bad_credential_shape_is_400() -> None:
    def open_scm(ref, s, token):
        raise SCMCredentialError('Bitbucket credential must be in "username:api_key" format (API key via Basic auth).')

    deps = ServerDeps(settings=Settings(_env_file=None), open_storage=lambda k: None, notify=lambda p, b: None, open_scm=open_scm)
    r = TestClient(create_app(Settings(_env_file=None), deps)).post(
        "/api/git/check-update", json={"repoUrl": "https://bitbucket.org/acme/w", "gitBranch": "main", "gitToken": "nocolon"},
    )
    assert r.status_code == 400 and "username:api_key" in r.json()["error"]


def test_non_object_body_is_400() -> None:
    r = _app(FakeClient()).post("/api/git/check-update", json=["nope"])
    assert r.status_code == 400 and r.json() == {"error": "Request body must be a JSON object"}


# --- primitives ---------------------------------------------------------------------


def test_latest_commit() -> None:
    fake = FakeClient()
    r = _app(fake).post("/api/git/latest-commit", json={"repoUrl": REPO, "gitBranch": "release/1"})
    assert r.status_code == 200
    assert r.json() == {"sha": SHA, "message": "feat: x", "author": "Alice", "date": "2026-09-01T00:00:00Z"}
    assert fake.calls == [("branch_head", "release/1")]


def test_compare() -> None:
    fake = FakeClient(changed=["a.py"], deleted=["b.py", "c.py"])
    r = _app(fake).post("/api/git/compare", json={"repoUrl": REPO, "baseCommitId": OLD, "headCommitId": SHA})
    assert r.status_code == 200
    assert r.json() == {"changedFiles": ["a.py"], "deletedFiles": ["b.py", "c.py"], "fileUpdatedCount": 1, "fileDeletedCount": 2}
    assert fake.calls == [("compare", OLD, SHA)]


def test_compare_requires_both_commits() -> None:
    r = _app(FakeClient()).post("/api/git/compare", json={"repoUrl": REPO, "baseCommitId": OLD})
    assert r.status_code == 400 and "headCommitId" in r.json()["error"]


def test_tree() -> None:
    fake = FakeClient(tree=["src/a.py", "README.md"])
    r = _app(fake).post("/api/git/tree", json={"repoUrl": REPO, "commitId": SHA})
    assert r.status_code == 200 and r.json() == {"files": ["src/a.py", "README.md"], "count": 2}
    assert fake.calls == [("tree", SHA)]


def test_token_is_not_required_and_passed_as_none_when_absent() -> None:
    seen: list = []
    _app(FakeClient(), seen=seen).post("/api/git/latest-commit", json={"repoUrl": REPO, "gitBranch": "main", "gitToken": ""})
    assert seen[0][1] is None


# --- pull-request -------------------------------------------------------------------


def test_pull_request_returns_camel_case_shape() -> None:
    fake = FakeClient()
    r = _app(fake).post("/api/git/pull-request", json={"repoUrl": REPO, "pullRequestId": 7, "gitToken": "t"})
    assert r.status_code == 200
    assert r.json() == {
        "number": 7, "title": "Add x", "state": "open",
        "baseBranch": "main", "headBranch": "feat/x",
        "baseCommitId": OLD, "headCommitId": SHA,
        "url": "https://github.com/acme/widgets/pull/7",
    }
    assert fake.calls == [("pull_request", 7)] and fake.closed


def test_pull_request_accepts_string_id() -> None:
    fake = FakeClient()
    assert _app(fake).post("/api/git/pull-request", json={"repoUrl": REPO, "pullRequestId": " 42 "}).status_code == 200
    assert fake.calls == [("pull_request", 42)]


@pytest.mark.parametrize(("raw", "msg"), [("abc", "must be an integer"), (0, "positive"), (-3, "positive")])
def test_pull_request_rejects_bad_id(raw, msg) -> None:
    r = _app(FakeClient()).post("/api/git/pull-request", json={"repoUrl": REPO, "pullRequestId": raw})
    assert r.status_code == 400 and msg in r.json()["error"]


def test_pull_request_requires_id() -> None:
    r = _app(FakeClient()).post("/api/git/pull-request", json={"repoUrl": REPO})
    assert r.status_code == 400 and "pullRequestId" in r.json()["error"]


def test_pull_request_provider_404_is_502() -> None:
    fake = FakeClient(fail=SCMAPIError("GitHub API GET /repos/acme/widgets/pulls/7 failed with HTTP 404: repository or commit not found, or the token cannot see it", http_status=404))
    r = _app(fake).post("/api/git/pull-request", json={"repoUrl": REPO, "pullRequestId": 7})
    assert r.status_code == 502 and "HTTP 404" in r.json()["error"]


# --- pr-comment ---------------------------------------------------------------------


def test_pr_comment_posts_and_returns_id_and_url() -> None:
    fake = FakeClient()
    r = _app(fake).post("/api/git/pr-comment", json={"repoUrl": REPO, "pullRequestId": 7, "body": "hello", "gitToken": "t"})
    assert r.status_code == 200
    assert r.json() == {"id": "123", "url": "https://github.com/acme/widgets/pull/7#issuecomment-123"}
    assert fake.calls == [("post_pr_comment", 7, "hello")] and fake.closed


def test_pr_comment_requires_body_and_id() -> None:
    r = _app(FakeClient()).post("/api/git/pr-comment", json={"repoUrl": REPO, "pullRequestId": 7})
    assert r.status_code == 400 and r.json() == {"error": "All fields required: repoUrl, body"}
    r = _app(FakeClient()).post("/api/git/pr-comment", json={"repoUrl": REPO, "body": "x"})
    assert r.status_code == 400 and "pullRequestId" in r.json()["error"]


def test_pr_comment_forbidden_is_502_with_scope_hint() -> None:
    fake = FakeClient(fail=SCMAPIError("GitHub API POST /repos/acme/widgets/issues/7/comments failed with HTTP 403: the credential lacks access; check the token scope (GitLab needs read_api) and repository permissions", http_status=403))
    r = _app(fake).post("/api/git/pr-comment", json={"repoUrl": REPO, "pullRequestId": 7, "body": "x"})
    assert r.status_code == 502 and "HTTP 403" in r.json()["error"]


# --- parse-pr-url -------------------------------------------------------------------


def test_parse_pr_url_returns_backend_shape_without_a_client() -> None:
    seen: list = []
    r = _app(FakeClient(), seen=seen).post("/api/git/parse-pr-url", json={"prUrl": "https://github.com/acme/widgets/pull/123"})
    assert r.status_code == 200
    api = "https://api.github.com/repos/acme/widgets"
    assert r.json() == {
        "source": "github", "repoUrl": "https://github.com/acme/widgets", "prId": "123",
        "prLinks": {"repo": "https://github.com/acme/widgets", "diff": f"{api}/pulls/123", "comment": f"{api}/issues/123/comments", "decline": f"{api}/pulls/123"},
    }
    assert seen == []


def test_parse_pr_url_self_hosted() -> None:
    settings = Settings(_env_file=None, scm_instances={"git.acme.com": "gitlab"}, scm_instance_base_url_mapping={"git.acme.com": "https://git.acme.com/api/v4"})
    r = _app(FakeClient(), settings).post("/api/git/parse-pr-url", json={"prUrl": "https://git.acme.com/grp/proj/-/merge_requests/4"})
    assert r.json()["repoUrl"] == "https://git.acme.com/grp/proj" and r.json()["prLinks"]["decline"].endswith("/merge_requests/4")


@pytest.mark.parametrize("url", ["https://example.com/acme/widgets/pull/1", "https://github.com/acme/widgets"])
def test_parse_pr_url_invalid_is_400(url) -> None:
    r = _app(FakeClient()).post("/api/git/parse-pr-url", json={"prUrl": url})
    assert r.status_code == 400 and "Invalid pull request URL" in r.json()["error"]


def test_parse_pr_url_requires_pr_url() -> None:
    r = _app(FakeClient()).post("/api/git/parse-pr-url", json={})
    assert r.status_code == 400 and r.json() == {"error": "All fields required: prUrl"}
