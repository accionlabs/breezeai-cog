"""Git source acquisition for ``/api/analyze-diff`` (server-only).

First-time analysis does a full ``git clone`` (avoids per-file API rate limits);
incremental analysis pulls only the changed files via the provider REST API. Provider
specifics live in ``integrations/scm/``; this module is the orchestration and the
``git`` subprocess fallback. Returns a populated temp dir + the changed-file filter set
+ the deleted-file list. The returned temp dir is the caller's to clean up.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..config import Settings
from ..integrations.scm import (
    AbstractSCMClient,
    RepoRef,
    SCMAPIError,
    SCMClientFactory,
    SCMError,
)
from ..integrations.scm.repository import parse_repo_url as _parse_repo_ref
from .errors import ApiError

logger = logging.getLogger(__name__)

INVALID_REPO_URL_MESSAGE = (
    "Invalid repo URL (supported hosts: github.com, bitbucket.org, gitlab.com, dev.azure.com, "
    "*.visualstudio.com, and any host listed in BREEZEAI_COG_SCM_INSTANCES)"
)


def parse_repo_url(
    repo_url: str, instances: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    """Legacy dict view of ``integrations.scm.repository.parse_repo_url`` for the route."""
    ref = _parse_repo_ref(repo_url, instances)
    return None if ref is None else ref.as_dict()


def _scrub(s: str) -> str:
    return re.sub(r"//[^/@\s]+:[^/@\s]+@", "//***:***@", str(s or ""))


def clone_repo_full(
    client: AbstractSCMClient,
    ref: RepoRef,
    incoming: str,
    branch: str,
    timeout: float = 1800.0,
) -> str:
    """Shallow single-branch clone of ``branch``, then check out ``incoming``.

    ``incoming`` is a commit SHA and must never be passed as ``--branch``; a shallow
    clone only has the branch tip, so the exact commit is fetched on demand.
    """
    temp_dir = tempfile.mkdtemp(prefix="ontology-clone-")
    auth_url = client.clone_url(ref)

    def git(*argv: str) -> None:
        subprocess.run(["git", *argv], check=True, capture_output=True, timeout=timeout)

    try:
        git("clone", "--depth", "1", "--branch", branch, "--single-branch", auth_url, temp_dir)
        if incoming:
            try:
                git("-C", temp_dir, "checkout", "--quiet", incoming)
            except subprocess.CalledProcessError:
                # Shallow clone only has the branch tip; fetch the exact commit.
                git("-C", temp_dir, "fetch", "--depth", "1", "origin", incoming)
                git("-C", temp_dir, "checkout", "--quiet", incoming)
    except subprocess.TimeoutExpired as exc:
        # `from None`: CalledProcessError/TimeoutExpired render argv, which carries the
        # auth-bearing clone URL. Do not chain them onto the traceback.
        shutil.rmtree(temp_dir, ignore_errors=True)  # a failed clone must not leak its dir
        raise RuntimeError(f"git clone timed out: {_scrub(str(exc))}") from None
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        stderr = exc.stderr.decode() if exc.stderr else str(exc)
        raise RuntimeError(f"git clone failed: {_scrub(stderr)}") from None
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    shutil.rmtree(Path(temp_dir) / ".git", ignore_errors=True)
    return temp_dir


#: Unchanged files whose content cross-file facts read when a file next to them changes.
#: Without them the skeleton holds them empty, and a fact proven there — a
#: Spring AI MCP server starter in a build file, a FastMCP server built in another module —
#: vanishes, so the changed file's tools would be dropped. Bounded: at most this many fetches
#: of each kind (build files, Python modules), so one kind cannot crowd out the other. When a
#: cap cuts, or a context file cannot be read, the incremental analysis is abandoned for a full
#: clone (:class:`IncompleteDiffContext`) rather than re-ingesting tools without their proof.
_MAX_CONTEXT_FILES = 50
_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
#: The prefilter of the Java and Kotlin MCP tool detectors (their ``Gate`` guard): a changed
#: file without it has no tool, so it needs no build file.
_JVM_TOOL_MARKER = "Tool"
#: ``@x.tool`` in a Python file: a FastMCP tool whose server may be imported.
_PY_TOOL = re.compile(r"@[ \t]*\w+[ \t]*\.[ \t]*tool\b")
_PY_FROM_IMPORT = re.compile(r"^[ \t]*from[ \t]+(\.*)([\w.]*)[ \t]+import\b", re.MULTILINE)
_PY_PROJECT_FILES = ("pyproject.toml", "setup.py", "setup.cfg")


class _TreeIndex:
    """The repo tree as sets of files and folders, built once: each lookup is a set probe, so
    resolving an import costs the depth of the importing file, not the size of the tree."""

    def __init__(self, tree: list[str]) -> None:
        self.files = set(tree)
        self.dirs: set[str] = {""}
        for path in tree:
            parts = path.split("/")[:-1]
            for n in range(1, len(parts) + 1):
                self.dirs.add("/".join(parts[:n]))

    @staticmethod
    def join(folder: str, *names: str) -> str:
        return "/".join(p for p in (folder, *names) if p)

    def has(self, folder: str, name: str) -> bool:
        path = self.join(folder, name)
        return path in self.files or path in self.dirs

    def module_file(self, folder: str, parts: list[str]) -> str | None:
        stem = self.join(folder, *parts)
        return next((c for c in (f"{stem}.py", f"{stem}/__init__.py") if c in self.files), None)


def _python_module_files(path: str, dots: str, module: str, index: _TreeIndex) -> list[str]:
    """The repo file a ``from <dots><module> import …`` in ``path`` resolves to, by the Python
    parser's rules (``parsers/python/imports.py``): relative to the file; else absolute from the
    repository root, then from the import roots above the file — linked only when exactly one
    place has it. ``[]`` when it does not resolve in the repo."""
    mod = [p for p in module.split(".") if p]
    base = path.split("/")[:-1]
    if dots:
        up = len(dots) - 1
        if up > len(base) or not (base[: len(base) - up] + mod):
            return []
        found = index.module_file("/".join(base[: len(base) - up]), mod)
        return [found] if found else []
    if not mod:
        return []
    found = index.module_file("", mod)
    if found:
        return [found]
    folder = "/".join(base)
    ancestors = ["/".join(base[:n]) for n in range(len(base), 0, -1)]
    first = mod[0]

    def holds(f: str) -> bool:
        return index.has(f, first) or index.has(f, f"{first}.py")

    roots: list[str] = []
    for f in ancestors:
        here = holds(f)
        src = index.has(f, "src") and holds(index.join(f, "src"))
        if not (here or src):
            continue
        if any(index.has(f, name) for name in _PY_PROJECT_FILES):
            if here:
                roots.append(f)
            if src and not index.has(index.join(f, "src"), "__init__.py"):
                roots.append(index.join(f, "src"))
        elif here and len(mod) > 1 and not index.has(f, "__init__.py"):
            roots.append(f)
    if (
        len(mod) == 1
        and folder not in roots
        and holds(folder)
        and not index.has(folder, "__init__.py")
    ):
        roots.insert(0, folder)  # a script: its own folder is on sys.path
    hits = {m for r in roots if (m := index.module_file(r, mod))}
    return list(hits) if len(hits) == 1 else []


class IncompleteDiffContext(Exception):
    """An incremental diff could not get every unchanged file its changed files' cross-file
    facts read (a cap cut, or a fetch failed). Parsing it would drop facts proven there — an
    MCP server's tools, re-ingested without their server — so the caller analyzes the whole
    commit instead. Carries the compare's ``deleted`` paths for that full run."""

    def __init__(self, reason: str, deleted: list[str]) -> None:
        super().__init__(reason)
        self.deleted = deleted


def _capped(paths: dict[str, None], kind: str) -> tuple[list[str], bool]:
    out = list(paths)
    cut = len(out) > _MAX_CONTEXT_FILES
    if cut:
        logger.warning(
            "Context fetch capped: %d %s needed, more than the %d an incremental analysis fetches",
            len(out),
            kind,
            _MAX_CONTEXT_FILES,
        )
    return out[:_MAX_CONTEXT_FILES], cut


def context_plan(changed: Mapping[str, str], tree: list[str]) -> tuple[list[str], bool]:
    """Unchanged files to fetch with real content for the facts the ``changed`` files (path →
    content) depend on, and whether that list is complete. Build files in the folder or an
    ancestor of a changed Java / Kotlin file that may declare an MCP tool — the repository root's first, then each changed file's
    nearest, then the folders between — and the modules a changed Python file with an
    ``@x.tool`` imports from. At most :data:`_MAX_CONTEXT_FILES` of each kind, in a stable
    order; ``False`` (and a logged warning) when a cap cut."""
    index = _TreeIndex(tree)
    unchanged = index.files - set(changed)
    root: dict[str, None] = {}
    nearest: dict[str, None] = {}
    between: dict[str, None] = {}
    modules: dict[str, None] = {}
    for path in sorted(changed):
        if path.endswith((".java", ".kt")) and _JVM_TOOL_MARKER in changed[path]:
            parts = path.split("/")[:-1]
            found = False
            for n in range(len(parts), -1, -1):
                folder = "/".join(parts[:n])
                here = [
                    c
                    for name in _BUILD_FILES
                    if (c := f"{folder}/{name}" if folder else name) in unchanged
                ]
                bucket = root if not folder else between if found else nearest
                for candidate in here:
                    bucket[candidate] = None
                found = found or bool(here)
        elif path.endswith(".py") and _PY_TOOL.search(changed[path]):
            for m in _PY_FROM_IMPORT.finditer(changed[path]):
                for candidate in _python_module_files(path, m.group(1), m.group(2), index):
                    if candidate in unchanged:
                        modules[candidate] = None
    build = {**root, **nearest, **between}
    build_paths, build_cut = _capped(build, "build files")
    module_paths, module_cut = _capped(modules, "Python modules")
    return build_paths + module_paths, not (build_cut or module_cut)


def context_paths(changed: Mapping[str, str], tree: list[str]) -> list[str]:
    """The files of :func:`context_plan`, without its completeness flag."""
    return context_plan(changed, tree)[0]


def resolve_git_diff(
    client: AbstractSCMClient,
    ref: RepoRef,
    current: str,
    incoming: str,
) -> tuple[str, set[str], list[str]]:
    """Incremental acquisition via the provider REST API.

    Returns ``(temp_dir, filter_set, deleted)``. The temp dir holds **every** blob path
    at ``incoming`` as an empty file (so the scanner and import resolution see the whole
    repo shape) with real content for the changed files and the few unchanged files their
    cross-file facts read (:func:`context_plan`). Raises :class:`IncompleteDiffContext` (temp
    dir removed) when those cannot all be fetched.
    """
    diff = client.compare(ref, current, incoming)

    if diff.is_empty:
        # The commit range touches no files at all — the usual cause is a merge commit
        # whose branch brought in no net change. The tree is identical to `current`, so
        # the stored graph is already correct: return an empty diff (not a full clone)
        # and let the caller write empty meta and advance the commit pointer. Distinct
        # from the `changed and not filter_set` case below, which means "changes exist
        # but we could not read them" and must fail loudly.
        return tempfile.mkdtemp(prefix="ontology-"), set(), []

    temp_dir = tempfile.mkdtemp(prefix="ontology-")
    tree = list(client.tree(ref, incoming))
    for sp in tree:
        full = Path(temp_dir) / sp
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("")

    filter_set: set[str] = set()
    contents: dict[str, str] = {}
    for path in diff.changed:
        try:
            content = client.file_content(ref, path, incoming)
        except SCMAPIError as exc:
            # Binary or genuinely unreadable files are expected to skip; log them so an
            # empty ingest (e.g. a token that lacks read access) is diagnosable rather
            # than silently reported as "no changes".
            logger.warning("Skipping unreadable changed file %s: %s", path, exc)
            continue
        full = Path(temp_dir) / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        filter_set.add(path)
        contents[path] = content

    if diff.changed and not filter_set:
        # The compare found changed files but none could be read. Failing keeps the
        # sync retryable; an empty result would let the backend advance the stored
        # commit onto a stale graph while reporting "already up to date".
        raise ApiError(
            "Detected changed files but none could be read from the provider — "
            "check the token scope (GitLab needs read_api) and repository access.",
            502,
        )

    # Real content for the unchanged files cross-file facts read; parsed, never streamed (they
    # are not in `filter_set`). One that is cut or cannot be read would stay an empty skeleton
    # and silently drop the facts proven there, so the diff is abandoned instead.
    paths, complete = context_plan(contents, tree)
    try:
        if not complete:
            raise IncompleteDiffContext("context files over the fetch cap", diff.deleted)
        for path in paths:
            try:
                content = client.file_content(ref, path, incoming)
            except SCMAPIError as exc:
                raise IncompleteDiffContext(
                    f"context file {path} could not be read: {exc}", diff.deleted
                ) from None
            (Path(temp_dir) / path).write_text(content, encoding="utf-8")
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return temp_dir, filter_set, diff.deleted


def acquire_diff(
    settings: Settings, body: dict[str, Any]
) -> tuple[str, set[str] | None, list[str]]:
    """Entry point behind ``ServerDeps.acquire_diff``.

    Incremental when a usable ``currentCommitId`` is present **and** the provider client
    supports it; otherwise a full clone (``filter_set=None`` → process every file). An
    incremental diff whose context files cannot all be fetched also falls back to a full clone,
    keeping the compare's deleted paths.
    """
    ref = _parse_repo_ref(body["repoUrl"], settings.scm_instances)
    if ref is None:
        raise ApiError(INVALID_REPO_URL_MESSAGE, 400)

    current = body.get("currentCommitId")
    incoming = body["incomingCommitId"]
    branch = body["gitBranch"]
    has_current = current not in (None, "", "null", "undefined")

    try:
        client = SCMClientFactory.for_repo(ref, settings, request_token=body.get("gitToken"))
    except SCMError as exc:
        raise ApiError(str(exc), exc.status_code) from None

    with client:
        try:
            deleted: list[str] = []
            if has_current and client.supports_incremental:
                try:
                    return resolve_git_diff(client, ref, str(current), incoming)
                except IncompleteDiffContext as exc:
                    # Analyze the whole commit: every file is parsed with its real neighbours.
                    # The compare's deletions still ride along, since a full run streams only
                    # the files that exist.
                    logger.warning("Incremental analysis abandoned for a full clone: %s", exc)
                    deleted = exc.deleted
            temp_dir = clone_repo_full(client, ref, incoming, branch, settings.git_clone_timeout)
            return temp_dir, None, deleted
        except SCMError as exc:
            raise ApiError(str(exc), exc.status_code) from None
