"""`server/git.py` orchestration over an injected SCM client: the incremental/clone gate,
the branch/commit argument split in the clone, skeleton + content materialisation, the
empty-compare and unreadable-changes cases, error mapping, and credential scrubbing."""

from __future__ import annotations

import subprocess
import traceback
from pathlib import Path

import pytest

from breezeai_cog.config import Settings
from breezeai_cog.integrations.scm import (
    AbstractSCMClient,
    ChangeSet,
    CommitInfo,
    PrComment,
    PullRequestInfo,
    RepoRef,
    SCMAPIError,
)
from breezeai_cog.server import git as git_mod
from breezeai_cog.server.errors import ApiError

_SHA = "01d9ad8afd9e6c6dc35882e947d86d357f1994a7"
_BRANCH = "main"
REF = RepoRef("github", "acme", "repo", host="github.com")


class FakeClient(AbstractSCMClient):
    """Scriptable provider: fixed tree / compare results, content map, clone URL."""

    provider = "github"

    def __init__(self, *, tree=(), changed=(), deleted=(), content=None, incremental=True,
                 token="tok") -> None:
        self._tree = list(tree)
        self._changes = ChangeSet(list(changed), list(deleted))
        self._content = dict(content or {})
        self.supports_incremental = incremental
        self._token = token
        self.calls: list[str] = []
        self.closed = False

    def tree(self, ref, commit):
        self.calls.append("tree")
        return self._tree

    def branch_head(self, ref, branch):
        self.calls.append(f"branch_head:{branch}")
        return CommitInfo(sha=_SHA, message="m", author="a", date="2026-01-01T00:00:00Z")

    def pull_request(self, ref, number):
        self.calls.append(f"pull_request:{number}")
        return PullRequestInfo(number, "t", "open", "main", "feat", "b" * 40, _SHA)

    def post_pr_comment(self, ref, number, body):
        self.calls.append(f"post_pr_comment:{number}")
        return PrComment("1", "")

    def compare(self, ref, base, head):
        self.calls.append("compare")
        return self._changes

    def file_content(self, ref, path, commit):
        self.calls.append(f"content:{path}")
        if path not in self._content:
            raise SCMAPIError(f"nope {path}", http_status=404)
        return self._content[path]

    def clone_url(self, ref):
        auth = f"x-access-token:{self._token}@" if self._token else ""
        return f"https://{auth}github.com/{ref.owner}/{ref.repo}.git"

    def close(self):
        self.closed = True


@pytest.fixture
def recorded_git(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture every git subprocess argv instead of running it."""
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _use_client(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    monkeypatch.setattr(
        git_mod.SCMClientFactory, "for_repo", staticmethod(lambda ref, settings, **kw: client)
    )


def _body(**over) -> dict:
    body = {
        "repoUrl": "https://github.com/acme/repo",
        "incomingCommitId": _SHA,
        "gitBranch": _BRANCH,
        "projectUuid": "u",
        "codeOntologyId": "c",
    }
    body.update(over)
    return body


# --- resolve_git_diff ---------------------------------------------------------------


def test_empty_compare_returns_an_empty_diff_without_cloning(recorded_git) -> None:
    """A no-op merge commit touches no files: nothing to analyze, so no clone. The route
    reads `filter_set == set()` as "deletion-only" and writes empty meta."""
    client = FakeClient(tree=["a.py"], changed=[], deleted=[])

    temp_dir, filter_set, deleted = git_mod.resolve_git_diff(client, REF, "base", _SHA)

    assert recorded_git == []
    assert filter_set == set() and deleted == []
    assert Path(temp_dir).is_dir()
    assert client.calls == ["compare"]  # no tree fetch for an empty diff


def test_incremental_materialises_skeleton_and_changed_content(recorded_git) -> None:
    client = FakeClient(
        tree=["src/a.py", "src/b.py", "README.md"],
        changed=["src/a.py", "src/new.py"], deleted=["gone.py"],
        content={"src/a.py": "x = 1\n", "src/new.py": "y = 2\n"},
    )

    temp_dir, filter_set, deleted = git_mod.resolve_git_diff(client, REF, "base", _SHA)

    root = Path(temp_dir)
    assert (root / "src/a.py").read_text() == "x = 1\n"
    assert (root / "src/new.py").read_text() == "y = 2\n"
    assert (root / "src/b.py").read_text() == "" and (root / "README.md").exists()
    assert filter_set == {"src/a.py", "src/new.py"}
    assert deleted == ["gone.py"]
    assert recorded_git == []


def test_unreadable_changed_file_is_skipped_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient(changed=["ok.py", "logo.png"], content={"ok.py": "ok\n"})
    with caplog.at_level("WARNING", logger="breezeai_cog.server.git"):
        _, filter_set, _ = git_mod.resolve_git_diff(client, REF, "base", _SHA)
    assert filter_set == {"ok.py"}
    assert any("logo.png" in r.getMessage() for r in caplog.records)


def test_all_changed_files_unreadable_is_a_502() -> None:
    client = FakeClient(changed=["a.py"], content={})
    with pytest.raises(ApiError) as info:
        git_mod.resolve_git_diff(client, REF, "base", _SHA)
    assert info.value.status_code == 502 and "read_api" in str(info.value)


# --- clone_repo_full ----------------------------------------------------------------


def test_clone_uses_branch_then_checks_out_the_sha(recorded_git) -> None:
    """`--branch` must receive the branch, never the SHA (git rejects a SHA there)."""
    temp_dir = git_mod.clone_repo_full(FakeClient(), REF, _SHA, _BRANCH)
    clone, checkout = recorded_git
    assert clone[:8] == ["git", "clone", "--depth", "1", "--branch", _BRANCH, "--single-branch",
                         "https://x-access-token:tok@github.com/acme/repo.git"]
    assert checkout == ["git", "-C", temp_dir, "checkout", "--quiet", _SHA]


def test_clone_failure_does_not_leak_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """CalledProcessError renders argv, which carries the auth URL — it must not ride
    along as the RuntimeError's chained context."""
    token = "ghp_averysecrettokenvalue"

    def fake_run(argv, **kwargs):
        raise subprocess.CalledProcessError(128, argv, stderr=b"fatal: Remote branch not found\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        git_mod.clone_repo_full(FakeClient(token=token), REF, _SHA, _BRANCH)

    rendered = "".join(traceback.format_exception(excinfo.value))
    assert token not in rendered
    assert "Remote branch not found" in rendered


# --- acquire_diff -------------------------------------------------------------------


def test_acquire_diff_first_analysis_clones_the_branch(monkeypatch, recorded_git) -> None:
    """No currentCommitId → full clone. This path was always correct; pin it."""
    client = FakeClient()
    _use_client(monkeypatch, client)

    temp_dir, filter_set, deleted = git_mod.acquire_diff(Settings(_env_file=None), _body())

    assert filter_set is None and deleted == []
    assert recorded_git[0][4:6] == ["--branch", _BRANCH]
    assert recorded_git[1][-1] == _SHA
    assert client.closed


@pytest.mark.parametrize("current", ["", "null", "undefined", None])
def test_acquire_diff_string_sentinels_mean_no_current(monkeypatch, recorded_git, current) -> None:
    _use_client(monkeypatch, FakeClient(changed=["a.py"], content={"a.py": ""}))
    body = _body(currentCommitId=current) if current is not None else _body()
    _, filter_set, _ = git_mod.acquire_diff(Settings(_env_file=None), body)
    assert filter_set is None and len(recorded_git) == 2


def test_acquire_diff_no_op_merge_commit_does_not_clone(monkeypatch, recorded_git) -> None:
    """The production failure: an incremental run whose compare reports no files."""
    _use_client(monkeypatch, FakeClient(changed=[], deleted=[]))

    temp_dir, filter_set, deleted = git_mod.acquire_diff(
        Settings(_env_file=None), _body(currentCommitId="3b1db00")
    )

    assert recorded_git == []
    assert filter_set == set() and deleted == []


def test_acquire_diff_non_incremental_client_clones_the_branch(monkeypatch, recorded_git) -> None:
    """A provider that cannot diff via REST falls back to a clone of the **branch**."""
    _use_client(monkeypatch, FakeClient(incremental=False, changed=["a.py"]))

    _, filter_set, _ = git_mod.acquire_diff(Settings(_env_file=None), _body(currentCommitId="base"))

    assert filter_set is None
    assert recorded_git[0][4:6] == ["--branch", _BRANCH]


def test_acquire_diff_invalid_url_is_400() -> None:
    with pytest.raises(ApiError) as info:
        git_mod.acquire_diff(Settings(_env_file=None), _body(repoUrl="https://example.com/x/y"))
    assert info.value.status_code == 400


def test_acquire_diff_self_hosted_instance_is_accepted_when_configured(monkeypatch, recorded_git) -> None:
    seen: list[RepoRef] = []

    def fake_for_repo(ref, settings, **kw):
        seen.append(ref)
        return FakeClient()

    monkeypatch.setattr(git_mod.SCMClientFactory, "for_repo", staticmethod(fake_for_repo))
    settings = Settings(_env_file=None, scm_instances={"git.acme.com": "gitlab"})
    git_mod.acquire_diff(settings, _body(repoUrl="https://git.acme.com/grp/proj"))
    assert seen == [RepoRef("gitlab", "grp", "proj", host="git.acme.com")]


def test_acquire_diff_bad_bitbucket_credential_is_400() -> None:
    with pytest.raises(ApiError) as info:
        git_mod.acquire_diff(
            Settings(_env_file=None),
            _body(repoUrl="https://bitbucket.org/acme/repo", gitToken="nocolon"),
        )
    assert info.value.status_code == 400 and "username:api_key" in str(info.value)


def test_acquire_diff_provider_api_failure_is_502(monkeypatch) -> None:
    class Failing(FakeClient):
        def compare(self, ref, base, head):
            raise SCMAPIError("GitHub API GET /compare failed with HTTP 403", http_status=403)

    _use_client(monkeypatch, Failing())
    with pytest.raises(ApiError) as info:
        git_mod.acquire_diff(Settings(_env_file=None), _body(currentCommitId="base"))
    assert info.value.status_code == 502 and "HTTP 403" in str(info.value)


def test_acquire_diff_forwards_request_token(monkeypatch, recorded_git) -> None:
    seen: dict = {}

    def fake_for_repo(ref, settings, request_token=None, **kw):
        seen["token"] = request_token
        return FakeClient()

    monkeypatch.setattr(git_mod.SCMClientFactory, "for_repo", staticmethod(fake_for_repo))
    git_mod.acquire_diff(Settings(_env_file=None), _body(gitToken="ghp_x"))
    assert seen == {"token": "ghp_x"}
