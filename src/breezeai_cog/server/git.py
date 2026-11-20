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
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import Settings
from ..integrations.scm import (
    AbstractSCMClient,
    RepoRef,
    SCMAPIError,
    SCMClientFactory,
    SCMError,
)
from ..integrations.scm.repository import parse_bitbucket_server_url
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


def provider_override(body: Mapping[str, Any]) -> str:
    """``body["gitProvider"]`` lower-cased, or ``""``.

    Only ``bitbucket_server`` is acted on. Every other slug is ignored rather than
    rejected: the four cloud providers are identified from the URL and always have been,
    so honouring a slug for them would be a second source of truth that can disagree
    with the host.
    """
    return str(body.get("gitProvider") or "").strip().lower()


def resolve_repo_ref(body: Mapping[str, Any], instances: Mapping[str, str]) -> RepoRef | None:
    """``body["repoUrl"]`` → ``RepoRef``, honouring an explicit ``gitProvider``.

    Bitbucket Data Center ships no public cloud, so no URL pattern can identify it and
    its host would otherwise have to be pre-registered in ``scm_instances``. The slug on
    the request says so directly, which is what lets a per-customer instance be analysed
    without deployment-wide config. Shared by ``/api/analyze-diff`` and ``/api/git/*`` so
    the two cannot drift apart.

    Returns:
        ``None`` when no provider claims the URL, including when the caller named
        ``bitbucket_server`` for a URL that has no Data Center anchor — a contradiction
        the route reports rather than silently resolving in the URL's favour.
    """
    repo_url = str(body["repoUrl"])
    if provider_override(body) == "bitbucket_server":
        return parse_bitbucket_server_url(repo_url)
    return _parse_repo_ref(repo_url, instances)


def _scrub(s: str) -> str:
    """Mask credentials in text that may reach a log or an error message.

    Two shapes: userinfo in a clone URL, and an ``Authorization`` header, which Bitbucket
    Data Center puts in the git process's argv for a bare-token clone. The userinfo
    halves use ``*`` rather than ``+`` so an empty username (``//:secret@``) still
    matches — it used not to, leaving the password in the clear.
    """
    out = re.sub(r"//[^/@\s]*:[^/@\s]*@", "//***:***@", str(s or ""))
    return re.sub(r"(?i)(authorization:\s*)(\S+\s+)?\S+", r"\1***", out)


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
        git(
            *client.git_auth_config(),
            "clone", "--depth", "1", "--branch", branch, "--single-branch",
            auth_url, temp_dir,
        )
        if incoming:
            try:
                git("-C", temp_dir, "checkout", "--quiet", incoming)
            except subprocess.CalledProcessError:
                # Shallow clone only has the branch tip; fetch the exact commit. The
                # auth config is repeated because a bare-token credential lives in a
                # header, not in the remote URL the clone stored.
                git(
                    "-C", temp_dir, *client.git_auth_config(),
                    "fetch", "--depth", "1", "origin", incoming,
                )
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


def _safe_join(root: Path, repo_path: str) -> Path:
    """``root / repo_path``, proven to stay inside ``root``.

    Every path here is a string a git host chose, and a caller can point this service at
    any host. A path like ``../../app/.venv/…/x.pth`` would otherwise be written outside
    the temp directory, where a ``.pth`` file is executed at the next interpreter start.
    Nothing legitimate needs to escape, so a path that does is a hard failure rather than
    a skip: a host sending one is not a host whose other answers should be trusted.

    Raises:
        SCMAPIError: ``repo_path`` is absolute, escapes ``root``, or is otherwise
            unusable as a relative path.
    """
    candidate = PurePosixPath(repo_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SCMAPIError(f"Unsafe path from the git provider: {repo_path!r}")
    full = (root / repo_path).resolve()
    if full != root and root not in full.parents:
        raise SCMAPIError(f"Unsafe path from the git provider: {repo_path!r}")
    return full


def resolve_git_diff(
    client: AbstractSCMClient,
    ref: RepoRef,
    current: str,
    incoming: str,
) -> tuple[str, set[str], list[str]]:
    """Incremental acquisition via the provider REST API.

    Returns ``(temp_dir, filter_set, deleted)``. The temp dir holds **every** blob path
    at ``incoming`` as an empty file (so the scanner and import resolution see the whole
    repo shape) with real content only for the changed files.
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
    root = Path(temp_dir).resolve()
    for sp in client.tree(ref, incoming):
        full = _safe_join(root, sp)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("")

    filter_set: set[str] = set()
    for path in diff.changed:
        try:
            content = client.file_content(ref, path, incoming)
        except SCMAPIError as exc:
            # Binary or genuinely unreadable files are expected to skip; log them so an
            # empty ingest (e.g. a token that lacks read access) is diagnosable rather
            # than silently reported as "no changes".
            logger.warning("Skipping unreadable changed file %s: %s", path, exc)
            continue
        full = _safe_join(root, path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
        filter_set.add(path)

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


def acquire_diff(settings: Settings, body: dict[str, Any]) -> tuple[str, set[str] | None, list[str]]:
    """Entry point behind ``ServerDeps.acquire_diff``.

    Incremental when a usable ``currentCommitId`` is present **and** the provider client
    supports it; otherwise a full clone (``filter_set=None`` → process every file).
    """
    ref = resolve_repo_ref(body, settings.scm_instances)
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
