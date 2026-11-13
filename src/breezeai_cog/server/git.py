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
#: of each kind (build files, Python modules), so one kind cannot crowd out the other.
_MAX_CONTEXT_FILES = 50
_BUILD_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
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


def _capped(paths: dict[str, None], kind: str) -> list[str]:
    out = list(paths)
    if len(out) > _MAX_CONTEXT_FILES:
        logger.warning(
            "Context fetch capped: %d %s needed, fetching the first %d; facts proven in the "
            "rest may be missing from this incremental analysis",
            len(out),
            kind,
            _MAX_CONTEXT_FILES,
        )
    return out[:_MAX_CONTEXT_FILES]


def context_paths(changed: Mapping[str, str], tree: list[str]) -> list[str]:
    """Unchanged files to fetch with real content for the facts the ``changed`` files (path →
    content) depend on: build files in a changed Java / Kotlin file's folder or an ancestor, and the
    modules a changed Python file with an ``@x.tool`` imports from. At most
    :data:`_MAX_CONTEXT_FILES` of each kind (a warning is logged when a cap cuts), in a stable
    order."""
    index = _TreeIndex(tree)
    unchanged = index.files - set(changed)
    build: dict[str, None] = {}
    modules: dict[str, None] = {}
    for path in sorted(changed):
        if path.endswith((".java", ".kt")):
            parts = path.split("/")[:-1]
            for n in range(len(parts), -1, -1):
                folder = "/".join(parts[:n])
                for name in _BUILD_FILES:
                    candidate = f"{folder}/{name}" if folder else name
                    if candidate in unchanged:
                        build[candidate] = None
        elif path.endswith(".py") and _PY_TOOL.search(changed[path]):
            for m in _PY_FROM_IMPORT.finditer(changed[path]):
                for candidate in _python_module_files(path, m.group(1), m.group(2), index):
                    if candidate in unchanged:
                        modules[candidate] = None
    return _capped(build, "build files") + _capped(modules, "Python modules")


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
    cross-file facts read (:func:`context_paths`).
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

    # Real content for the unchanged files cross-file facts read; parsed, never streamed (they
    # are not in `filter_set`). A file that cannot be read stays empty, as before.
    for path in context_paths(contents, tree):
        try:
            content = client.file_content(ref, path, incoming)
        except SCMAPIError as exc:
            logger.warning("Skipping unreadable context file %s: %s", path, exc)
            continue
        (Path(temp_dir) / path).write_text(content, encoding="utf-8")

    if diff.changed and not filter_set:
        # The compare found changed files but none could be read. Failing keeps the
        # sync retryable; an empty result would let the backend advance the stored
        # commit onto a stale graph while reporting "already up to date".
        raise ApiError(
            "Detected changed files but none could be read from the provider — "
            "check the token scope (GitLab needs read_api) and repository access.",
            502,
        )

    return temp_dir, filter_set, diff.deleted


def acquire_diff(
    settings: Settings, body: dict[str, Any]
) -> tuple[str, set[str] | None, list[str]]:
    """Entry point behind ``ServerDeps.acquire_diff``.

    Incremental when a usable ``currentCommitId`` is present **and** the provider client
    supports it; otherwise a full clone (``filter_set=None`` → process every file).
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
            if has_current and client.supports_incremental:
                return resolve_git_diff(client, ref, str(current), incoming)
            temp_dir = clone_repo_full(client, ref, incoming, branch, settings.git_clone_timeout)
            return temp_dir, None, []
        except SCMError as exc:
            raise ApiError(str(exc), exc.status_code) from None
