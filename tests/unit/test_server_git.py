"""`server/git.py` source-acquisition internals: the branch/commit argument split in the
full-clone fallback, and credential scrubbing on subprocess failure."""

from __future__ import annotations

import subprocess
import traceback
from pathlib import Path

import pytest

from breezeai_cog.config import Settings
from breezeai_cog.server import git as git_mod

_SHA = "01d9ad8afd9e6c6dc35882e947d86d357f1994a7"
_BRANCH = "main"


@pytest.fixture
def recorded_clone(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture clone_repo_full's arguments instead of cloning."""
    calls: list[dict] = []

    def fake_clone(provider, owner, project, repo, incoming, branch, token, timeout=1800.0):
        calls.append({"incoming": incoming, "branch": branch})
        return "/tmp/fake-clone"

    monkeypatch.setattr(git_mod, "clone_repo_full", fake_clone)
    return calls


def _no_diff_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """A compare that reports nothing changed and nothing deleted — the fallback trigger."""
    monkeypatch.setattr(git_mod, "_provider", lambda provider: {
        "tree": lambda o, r, c, t: [],
        "compare": lambda o, r, b, h, t: {"deleted": [], "changed": []},
        "content": lambda o, r, p, c, t: "",
    })


def test_empty_compare_returns_an_empty_diff_without_cloning(
    monkeypatch: pytest.MonkeyPatch, recorded_clone: list[dict]
) -> None:
    """A no-op merge commit touches no files: nothing to analyze, so no clone. The route
    reads `filter_set == set()` as "deletion-only" and writes empty meta."""
    _no_diff_provider(monkeypatch)

    temp_dir, filter_set, deleted = git_mod.resolve_git_diff(
        "github", "acme", "", "repo", "base", _SHA, _BRANCH, None
    )

    assert recorded_clone == []
    assert filter_set == set()
    assert deleted == []
    assert Path(temp_dir).is_dir()


def test_gitlab_fallback_clones_the_branch_not_the_sha(
    monkeypatch: pytest.MonkeyPatch, recorded_clone: list[dict]
) -> None:
    monkeypatch.setattr(git_mod, "_provider", lambda provider: {
        "tree": lambda o, r, c, t: [],
        "compare": lambda o, r, b, h, t: {"deleted": [], "changed": ["a.py"]},
        "content": lambda o, r, p, c, t: "",
    })

    git_mod.resolve_git_diff("gitlab", "acme", "", "repo", "base", _SHA, _BRANCH, None)

    assert recorded_clone == [{"incoming": _SHA, "branch": _BRANCH}]


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


def test_acquire_diff_first_analysis_clones_the_branch(
    monkeypatch: pytest.MonkeyPatch, recorded_clone: list[dict]
) -> None:
    """No currentCommitId → full clone. This path was always correct; pin it."""
    git_mod.acquire_diff(Settings(), _body())

    assert recorded_clone == [{"incoming": _SHA, "branch": _BRANCH}]


def test_acquire_diff_no_op_merge_commit_does_not_clone(
    monkeypatch: pytest.MonkeyPatch, recorded_clone: list[dict]
) -> None:
    """The production failure: an incremental run whose compare reports no files."""
    _no_diff_provider(monkeypatch)

    temp_dir, filter_set, deleted = git_mod.acquire_diff(
        Settings(), _body(currentCommitId="3b1db00")
    )

    assert recorded_clone == []
    assert filter_set == set()
    assert deleted == []


def test_clone_failure_does_not_leak_the_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """CalledProcessError renders argv, which carries the auth URL — it must not ride
    along as the RuntimeError's chained context."""
    token = "ghp_averysecrettokenvalue"
    argv = ["git", "clone", "--branch", _BRANCH,
            f"https://x-access-token:{token}@github.com/acme/repo.git", "/tmp/x"]

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(128, argv, stderr=b"fatal: Remote branch not found\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        git_mod.clone_repo_full("github", "acme", "", "repo", _SHA, _BRANCH, token)

    rendered = "".join(traceback.format_exception(excinfo.value))
    assert token not in rendered
    assert "Remote branch not found" in rendered
