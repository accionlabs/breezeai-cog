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
    FileDiff,
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
    """Scriptable provider: fixed tree / compare results, content map, clone URL. ``old`` is the
    content at the ``base`` commit (default: ``content``); ``files`` the compare's entries."""

    provider = "github"

    def __init__(self, *, tree=(), changed=(), deleted=(), content=None, incremental=True,
                 token="tok", old=None, files=()) -> None:
        self._tree = list(tree)
        self._changes = ChangeSet(list(changed), list(deleted), list(files))
        self._content = dict(content or {})
        self._old = self._content if old is None else dict(old)
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
        self.calls.append(f"content:{path}" if commit != "base" else f"old:{path}")
        content = self._old if commit == "base" else self._content
        if path not in content:
            raise SCMAPIError(f"nope {path}", http_status=404)
        return content[path]

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


def test_incremental_fetches_the_context_files_cross_file_facts_read(recorded_git) -> None:
    """A changed tools file keeps its MCP server proof — the module's build file
    and the Python module that builds the server get real content, but are not streamed."""
    tools_py = "from .server import mcp\n\n@mcp.tool\ndef f():\n    pass\n"
    client = FakeClient(
        tree=[
            "pom.xml", "svc/pom.xml", "svc/src/main/java/T.java", "other/pom.xml",
            "app/server.py", "app/tools.py", "app/util.py",
        ],
        changed=["svc/src/main/java/T.java", "app/tools.py"],
        content={
            "svc/src/main/java/T.java": "class T { @Tool String f() { return \"\"; } }\n",
            "app/tools.py": tools_py,
            "pom.xml": "<project/>",
            "svc/pom.xml": "<project>mcp</project>",
            "app/server.py": "mcp = FastMCP('x')\n",
        },
    )

    temp_dir, filter_set, _ = git_mod.resolve_git_diff(client, REF, "base", _SHA)

    root = Path(temp_dir)
    assert (root / "svc/pom.xml").read_text() == "<project>mcp</project>"
    assert (root / "pom.xml").read_text() == "<project/>"
    assert (root / "app/server.py").read_text() == "mcp = FastMCP('x')\n"
    assert (root / "other/pom.xml").read_text() == "" and (root / "app/util.py").read_text() == ""
    assert filter_set == {"svc/src/main/java/T.java", "app/tools.py"}  # context is not streamed
    fetched = {c for c in client.calls if c.startswith("content:")}
    assert "content:other/pom.xml" not in fetched and "content:app/util.py" not in fetched


def test_context_paths_are_bounded_and_skip_plain_python() -> None:
    tree = [f"m{i}/pom.xml" for i in range(80)] + [f"m{i}/A.java" for i in range(80)]
    changed = {f"m{i}/A.java": "@Tool" for i in range(80)}
    assert len(git_mod.context_paths(changed, tree)) == git_mod._MAX_CONTEXT_FILES
    tree = ["app/server.py", "app/x.py"]
    assert git_mod.context_paths({"app/x.py": "from .server import mcp\n"}, tree) == []
    assert git_mod.context_paths(
        {"app/x.py": "from app.server import mcp\n@mcp.tool\ndef f(): ...\n"}, tree
    ) == ["app/server.py"]


def test_context_paths_fetch_gradle_kts_for_a_kotlin_file() -> None:
    tree = ["build.gradle.kts", "svc/build.gradle.kts", "svc/src/main/kotlin/T.kt", "other/pom.xml"]
    changed = {"svc/src/main/kotlin/T.kt": "class T { @Tool fun f() = \"\" }\n"}
    assert git_mod.context_paths(changed, tree) == ["build.gradle.kts", "svc/build.gradle.kts"]


def test_context_paths_fetch_build_files_only_for_a_possible_mcp_tool() -> None:
    """A Java / Kotlin change without the MCP detectors' ``Tool`` marker has no tool, so it
    needs no build file; one with ``@Tool`` (or ``ToolSpecification``) still does."""
    tree = ["pom.xml", "svc/pom.xml", "svc/A.java", "svc/B.java", "svc/C.kt", "svc/build.gradle"]
    plain = {"svc/A.java": "class A { void f() {} }\n", "svc/C.kt": "class C\n"}
    assert git_mod.context_plan(plain, tree) == ([], True)
    tool = {**plain, "svc/B.java": "class B { @Tool String f() { return \"\"; } }\n"}
    assert git_mod.context_paths(tool, tree) == ["pom.xml", "svc/pom.xml", "svc/build.gradle"]


def test_context_paths_put_the_root_and_nearest_build_files_first() -> None:
    """The root build file, then each changed file's nearest, then the folders between."""
    tree = ["pom.xml", "a/pom.xml", "a/b/pom.xml", "a/b/c/X.java", "d/pom.xml", "d/Y.java"]
    changed = {"a/b/c/X.java": "@Tool", "d/Y.java": "@McpTool"}
    assert git_mod.context_plan(changed, tree) == (
        ["pom.xml", "a/b/pom.xml", "d/pom.xml", "a/pom.xml"],
        True,
    )


def test_context_paths_build_files_have_their_own_budget_and_caps_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """80 Java modules fill the build-file budget; the FastMCP server module a changed tools
    file imports is still fetched, and each truncation is logged."""
    tree = [f"m{i:02}/pom.xml" for i in range(80)] + [f"m{i:02}/A.java" for i in range(80)]
    tree += ["app/server.py", "app/tools.py"]
    changed = {f"m{i:02}/A.java": "@Tool" for i in range(80)}
    changed["app/tools.py"] = "from app.server import mcp\n@mcp.tool\ndef f(): ...\n"
    with caplog.at_level("WARNING", logger=git_mod.__name__):
        paths = git_mod.context_paths(changed, tree)
    assert "app/server.py" in paths
    assert len([p for p in paths if p.endswith("pom.xml")]) == git_mod._MAX_CONTEXT_FILES
    assert paths[0] == "m00/pom.xml"  # stable order
    assert git_mod.context_plan(changed, tree)[1] is False  # the cut is reported
    assert any("80 build files" in r.getMessage() for r in caplog.records)
    caplog.clear()
    with caplog.at_level("WARNING", logger=git_mod.__name__):
        git_mod.context_paths({"app/tools.py": changed["app/tools.py"]}, tree)
    assert not caplog.records  # under both caps: no warning


def test_context_over_the_cap_abandons_the_diff(recorded_git) -> None:
    """More build files than the cap: parsing would leave some empty and drop the facts proven
    there, so the diff is abandoned (temp dir removed) with the compare's deletions."""
    tree = [f"m{i:02}/pom.xml" for i in range(60)] + [f"m{i:02}/A.java" for i in range(60)]
    content = {f"m{i:02}/A.java": "class A { @Tool void f() {} }\n" for i in range(60)}
    content.update({f"m{i:02}/pom.xml": "<project/>" for i in range(60)})
    client = FakeClient(tree=tree, changed=list(content)[:60], deleted=["old.py"], content=content)
    made: list[str] = []
    real_mkdtemp = git_mod.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real_mkdtemp(**kw))
        return made[-1]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(git_mod.tempfile, "mkdtemp", mkdtemp)
        with pytest.raises(git_mod.IncompleteDiffContext) as info:
            git_mod.resolve_git_diff(client, REF, "base", _SHA)
    assert info.value.deleted == ["old.py"]
    assert not Path(made[0]).exists()
    assert not any(c.startswith("content:m") and c.endswith("pom.xml") for c in client.calls)


def test_unreadable_context_file_abandons_the_diff() -> None:
    """A context file that cannot be fetched would stay an empty skeleton: the changed tools
    file would lose its server proof, so the diff is abandoned rather than parsed."""
    client = FakeClient(
        tree=["app/server.py", "app/tools.py"],
        changed=["app/tools.py"],
        content={"app/tools.py": "from .server import mcp\n@mcp.tool\ndef f(): ...\n"},
    )
    with pytest.raises(git_mod.IncompleteDiffContext, match="app/server.py"):
        git_mod.resolve_git_diff(client, REF, "base", _SHA)


def test_acquire_diff_falls_back_to_a_full_clone_when_context_is_incomplete(
    monkeypatch, recorded_git
) -> None:
    client = FakeClient(
        tree=["app/server.py", "app/tools.py"],
        changed=["app/tools.py"],
        deleted=["app/old.py"],
        content={"app/tools.py": "from .server import mcp\n@mcp.tool\ndef f(): ...\n"},
    )
    _use_client(monkeypatch, client)

    _, filter_set, deleted = git_mod.acquire_diff(
        Settings(_env_file=None), _body(currentCommitId="base")
    )

    assert filter_set is None  # every file is parsed and streamed
    assert deleted == ["app/old.py"]
    assert recorded_git[0][4:6] == ["--branch", _BRANCH]


_POM = "<project><dependencies>{}</dependencies></project>"
_STARTER = "<dependency><artifactId>spring-ai-starter-mcp-server</artifactId></dependency>"
_JAVA_TOOL = "class T { @Tool String f() { return \"\"; } }\n"


def test_a_build_file_that_adds_the_mcp_server_starter_abandons_the_diff() -> None:
    """Only pom.xml changes: the unchanged @Tool file becomes an MCP tool, which an incremental
    run would never re-parse."""
    client = FakeClient(
        tree=["pom.xml", "src/T.java"],
        changed=["pom.xml"],
        content={"pom.xml": _POM.format(_STARTER), "src/T.java": _JAVA_TOOL},
        old={"pom.xml": _POM.format(""), "src/T.java": _JAVA_TOOL},
    )
    with pytest.raises(git_mod.IncompleteDiffContext, match="pom.xml") as info:
        git_mod.resolve_git_diff(client, REF, "base", _SHA)
    assert info.value.deleted == []


def test_a_build_file_that_drops_the_mcp_server_starter_abandons_the_diff() -> None:
    client = FakeClient(
        tree=["pom.xml", "src/T.java"],
        changed=["pom.xml"],
        content={"pom.xml": _POM.format("")},
        old={"pom.xml": _POM.format(_STARTER)},
    )
    with pytest.raises(git_mod.IncompleteDiffContext, match="pom.xml"):
        git_mod.resolve_git_diff(client, REF, "base", _SHA)


def test_acquire_diff_clones_when_only_the_build_file_enables_mcp(monkeypatch, recorded_git) -> None:
    client = FakeClient(
        tree=["pom.xml", "src/T.java"],
        changed=["pom.xml"],
        deleted=["old/pom.xml"],
        content={"pom.xml": _POM.format(_STARTER)},
        old={"pom.xml": _POM.format("")},
    )
    _use_client(monkeypatch, client)

    _, filter_set, deleted = git_mod.acquire_diff(
        Settings(_env_file=None), _body(currentCommitId="base")
    )

    assert filter_set is None and deleted == ["old/pom.xml"]
    assert recorded_git[0][4:6] == ["--branch", _BRANCH]


def test_a_build_file_change_without_an_mcp_server_starter_stays_incremental() -> None:
    """A dependency bump in a plain Spring module: no version mentions the starter, and the
    compare's patch shows it, so the previous version is not even fetched."""
    dep = "<dependency><artifactId>spring-boot-starter-web</artifactId><version>{}</version></dependency>"
    client = FakeClient(
        tree=["pom.xml", "src/T.java"],
        changed=["pom.xml"],
        content={"pom.xml": _POM.format(dep.format("3.3.1"))},
        old={"pom.xml": _POM.format(dep.format("3.3.0"))},
        files=[FileDiff("pom.xml", "modified", 1, 1, "@@ -1 +1 @@\n-3.3.0\n+3.3.1\n")],
    )
    _, filter_set, _ = git_mod.resolve_git_diff(client, REF, "base", _SHA)
    assert filter_set == {"pom.xml"}
    assert "old:pom.xml" not in client.calls


def test_a_build_file_edit_that_keeps_the_mcp_server_starter_stays_incremental() -> None:
    client = FakeClient(
        tree=["pom.xml", "src/T.java"],
        changed=["pom.xml"],
        content={"pom.xml": _POM.format(_STARTER + "<dependency>b</dependency>")},
        old={"pom.xml": _POM.format(_STARTER)},
    )
    _, filter_set, _ = git_mod.resolve_git_diff(client, REF, "base", _SHA)
    assert filter_set == {"pom.xml"} and "old:pom.xml" in client.calls


_SERVER_PY = "from fastmcp import FastMCP\n\nmcp = FastMCP('x')\n"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("x = 1\n", _SERVER_PY),  # the server appears
        (_SERVER_PY, "x = 1\n"),  # the server goes, with every FastMCP mention
        (_SERVER_PY, _SERVER_PY.replace("mcp =", "app =")),  # the server is renamed
    ],
)
def test_a_python_module_whose_fastmcp_server_changes_abandons_the_diff(old, new) -> None:
    client = FakeClient(
        tree=["app/server.py", "app/tools.py"],
        changed=["app/server.py"],
        content={"app/server.py": new},
        old={"app/server.py": old},
    )
    with pytest.raises(git_mod.IncompleteDiffContext, match="app/server.py"):
        git_mod.resolve_git_diff(client, REF, "base", _SHA)


def test_a_deleted_fastmcp_server_module_abandons_the_diff() -> None:
    client = FakeClient(
        tree=["app/tools.py", "app/x.py"],
        changed=["app/x.py"],
        deleted=["app/server.py"],
        content={"app/x.py": "x = 1\n"},
        old={"app/server.py": _SERVER_PY, "app/x.py": "x = 2\n"},
    )
    with pytest.raises(git_mod.IncompleteDiffContext, match="app/server.py") as info:
        git_mod.resolve_git_diff(client, REF, "base", _SHA)
    assert info.value.deleted == ["app/server.py"]


def test_an_edit_that_keeps_the_fastmcp_server_stays_incremental() -> None:
    client = FakeClient(
        tree=["app/server.py"],
        changed=["app/server.py"],
        content={"app/server.py": _SERVER_PY + "\n@mcp.tool\ndef f(): ...\n"},
        old={"app/server.py": _SERVER_PY},
    )
    _, filter_set, _ = git_mod.resolve_git_diff(client, REF, "base", _SHA)
    assert filter_set == {"app/server.py"}


def test_an_unreadable_previous_version_abandons_the_diff() -> None:
    class Flaky(FakeClient):
        def file_content(self, ref, path, commit):
            if commit == "base":
                raise SCMAPIError("rate limited", http_status=429)
            return super().file_content(ref, path, commit)

    client = Flaky(tree=["pom.xml"], changed=["pom.xml"], content={"pom.xml": _POM.format("")})
    with pytest.raises(git_mod.IncompleteDiffContext, match="could not be read"):
        git_mod.resolve_git_diff(client, REF, "base", _SHA)


def test_context_paths_follow_the_python_import_rules() -> None:
    tools = "from {} import mcp\n@mcp.tool\ndef f(): ...\n"
    tree = [
        "svc/app/__init__.py", "svc/app/tools.py", "svc/app/server.py",
        "other/server.py", "vendor/lib/app/server.py", "proj/pyproject.toml",
        "proj/src/core/server.py", "proj/src/core/tools.py",
    ]
    # Absolute `server` from a package file: not the unrelated `other/server.py`.
    assert git_mod.context_paths({"svc/app/tools.py": tools.format("server")}, tree) == []
    # `app.server` resolves under the import root `svc/` only, not by any path suffix.
    assert git_mod.context_paths({"svc/app/tools.py": tools.format("app.server")}, tree) == [
        "svc/app/server.py"
    ]
    # A declared project's `src/` is an import root.
    assert git_mod.context_paths(
        {"proj/src/core/tools.py": tools.format("core.server")}, tree
    ) == ["proj/src/core/server.py"]


def test_context_paths_scale_with_import_depth_not_tree_size() -> None:
    import time

    tree = [f"pkg{i}/mod{j}/file{k}.py" for i in range(50) for j in range(50) for k in range(20)]
    changed = {
        f"pkg{i}/mod0/file0.py": "".join(f"from pkg{i}.mod{j} import x\n" for j in range(30))
        + "@mcp.tool\ndef f(): ...\n"
        for i in range(50)
    }
    start = time.perf_counter()
    git_mod.context_paths(changed, tree)
    assert time.perf_counter() - start < 2.0


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


def test_clone_failure_removes_its_temp_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A failed clone must not leave ``ontology-clone-*`` behind (pre-existing leak)."""
    import tempfile

    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, dir=str(tmp_path), **{k: v for k, v in kwargs.items() if k != "dir"})
        created.append(d)
        return d

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(128, ["git", "clone"], stderr=b"fatal: nope\n")

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="git clone failed"):
        git_mod.clone_repo_full(FakeClient(), REF, _SHA, _BRANCH)

    assert len(created) == 1
    assert not Path(created[0]).exists()


def test_clone_timeout_removes_its_temp_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import tempfile

    created: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, dir=str(tmp_path), **{k: v for k, v in kwargs.items() if k != "dir"})
        created.append(d)
        return d

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(["git", "clone"], 1)

    monkeypatch.setattr(tempfile, "mkdtemp", tracking_mkdtemp)
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="git clone timed out"):
        git_mod.clone_repo_full(FakeClient(), REF, _SHA, _BRANCH)

    assert not Path(created[0]).exists()
