"""``/api/git/*`` — the provider operations exposed over HTTP for the Breeze backend.

The backend's ``GET /code-ontology/check-update/:id`` used to run its own per-provider git
calls. Those now live in ``integrations/scm/`` here, so the backend calls these endpoints
instead and keeps one provider implementation across both services.

All four are ``POST`` with a JSON body (the credential must not ride in a query string),
run the provider calls in the threadpool, and map ``SCMError`` onto the ``{"error": …}``
contract: 400 for a bad URL or credential shape, 502 for a provider failure. Like
``/api/analyze-diff`` they carry no auth of their own — this service sits behind the Kong
ingress and is called server-to-server.

| Route | Body | Returns |
|---|---|---|
| ``/api/git/check-update`` | ``repoUrl``, ``gitBranch``, ``gitToken?``, ``currentCommitId?`` | the backend's check-update shape (``change``, ``latestCommitId``, ``currentCommitId``, ``fileUpdatedCount``, ``fileDeletedCount``) plus ``latestCommit`` and the file lists |
| ``/api/git/latest-commit`` | ``repoUrl``, ``gitBranch``, ``gitToken?`` | ``{sha, message, author, date}`` |
| ``/api/git/compare`` | ``repoUrl``, ``baseCommitId``, ``headCommitId``, ``gitToken?`` | ``{changedFiles, deletedFiles, fileUpdatedCount, fileDeletedCount}`` |
| ``/api/git/tree`` | ``repoUrl``, ``commitId``, ``gitToken?`` | ``{files, count}`` |
| ``/api/git/pull-request`` | ``repoUrl``, ``pullRequestId``, ``gitToken?`` | ``{number, title, state, baseBranch, headBranch, baseCommitId, headCommitId, url}`` |
| ``/api/git/pr-comment`` | ``repoUrl``, ``pullRequestId``, ``body``, ``gitToken?`` | ``{id, url}`` — **the one write**; not retried |
| ``/api/git/parse-pr-url`` | ``prUrl`` | ``{source, repoUrl, prId, prLinks: {repo, diff, comment, decline}}`` — no provider call |
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any, TypeVar

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from ..config import Settings
from ..integrations.scm import AbstractSCMClient, RepoRef, SCMError
from ..integrations.scm.pr_url import parse_pr_url
from ..integrations.scm.repository import parse_repo_url
from .deps import ServerDeps
from .errors import ApiError
from .git import INVALID_REPO_URL_MESSAGE

router = APIRouter(prefix="/api/git")

T = TypeVar("T")

#: ``currentCommitId`` values the backend sends when it has no stored commit yet — the same
#: string sentinels ``acquire_diff`` honours.
_NO_COMMIT = (None, "", "null", "undefined")


def _require(body: dict[str, Any], *fields: str) -> None:
    missing = [f for f in fields if not body.get(f)]
    if missing:
        raise ApiError(f"All fields required: {', '.join(fields)}", 400)


def _ref(settings: Settings, body: dict[str, Any]) -> RepoRef:
    ref = parse_repo_url(str(body["repoUrl"]), settings.scm_instances)
    if ref is None:
        raise ApiError(INVALID_REPO_URL_MESSAGE, 400)
    return ref


def _with_client(
    deps: ServerDeps, settings: Settings, body: dict[str, Any], fn: Callable[[AbstractSCMClient, RepoRef], T]
) -> T:
    """Build the provider client for ``body["repoUrl"]``, run ``fn``, close, map errors."""
    ref = _ref(settings, body)
    if deps.open_scm is None:  # pragma: no cover — default_deps always sets it
        raise ApiError("SCM client factory is not configured", 500)
    token = body.get("gitToken") or None
    try:
        with deps.open_scm(ref, settings, token) as client:
            return fn(client, ref)
    except SCMError as exc:
        raise ApiError(str(exc), exc.status_code) from None


async def _body(request: Request) -> dict[str, Any]:
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError("Request body must be a JSON object", 400)
    return body


@router.post("/parse-pr-url")
async def parse_pr_url_route(request: Request) -> dict[str, Any]:
    """Derive what the backend's manual PR trigger needs from a pasted PR URL: the
    provider (``source``), the repository's canonical web URL, the PR number and the
    ``prLinks`` block (repo web URL + provider REST URLs for diff / comment / decline).
    **No provider call, no token.** Unknown host or no PR number → 400."""
    settings: Settings = request.app.state.settings
    body = await _body(request)
    _require(body, "prUrl")
    info = parse_pr_url(str(body["prUrl"]), settings)
    if info is None:
        raise ApiError(
            "Invalid pull request URL: expected a supported host and a PR / MR number in the path",
            400,
        )
    return {
        "source": info.source,
        "repoUrl": info.repo_url,
        "prId": info.pr_id,
        "prLinks": {
            "repo": info.links.repo,
            "diff": info.links.diff,
            "comment": info.links.comment,
            "decline": info.links.decline,
        },
    }


@router.post("/latest-commit")
async def latest_commit(request: Request) -> dict[str, Any]:
    deps: ServerDeps = request.app.state.deps
    settings: Settings = request.app.state.settings
    body = await _body(request)
    _require(body, "repoUrl", "gitBranch")
    info = await run_in_threadpool(
        _with_client, deps, settings, body, lambda c, ref: c.branch_head(ref, str(body["gitBranch"]))
    )
    return asdict(info)


@router.post("/compare")
async def compare(request: Request) -> dict[str, Any]:
    deps: ServerDeps = request.app.state.deps
    settings: Settings = request.app.state.settings
    body = await _body(request)
    _require(body, "repoUrl", "baseCommitId", "headCommitId")
    diff = await run_in_threadpool(
        _with_client, deps, settings, body,
        lambda c, ref: c.compare(ref, str(body["baseCommitId"]), str(body["headCommitId"])),
    )
    return {
        "changedFiles": diff.changed,
        "deletedFiles": diff.deleted,
        "fileUpdatedCount": len(diff.changed),
        "fileDeletedCount": len(diff.deleted),
    }


@router.post("/tree")
async def tree(request: Request) -> dict[str, Any]:
    deps: ServerDeps = request.app.state.deps
    settings: Settings = request.app.state.settings
    body = await _body(request)
    _require(body, "repoUrl", "commitId")
    files = await run_in_threadpool(
        _with_client, deps, settings, body, lambda c, ref: c.tree(ref, str(body["commitId"]))
    )
    return {"files": files, "count": len(files)}


@router.post("/pull-request")
async def pull_request(request: Request) -> dict[str, Any]:
    """Read-only PR metadata with **full** base/head SHAs, normalised ``state``
    (``open`` / ``merged`` / ``closed``) and bare branch names. Feed ``baseCommitId`` /
    ``headCommitId`` to ``/compare`` for the PR's changed files."""
    deps: ServerDeps = request.app.state.deps
    settings: Settings = request.app.state.settings
    body = await _body(request)
    _require(body, "repoUrl")
    number = _pr_id(body)
    pr = await run_in_threadpool(
        _with_client, deps, settings, body, lambda c, ref: c.pull_request(ref, number)
    )
    return _pr_dict(pr)


def _pr_id(body: dict[str, Any]) -> int:
    raw = body.get("pullRequestId")
    if raw is None or raw == "":  # not `_require`: 0 is a value here, just an invalid one
        raise ApiError("All fields required: repoUrl, pullRequestId", 400)
    try:
        number = int(str(raw).strip())
    except ValueError:
        raise ApiError("pullRequestId must be an integer", 400) from None
    if number <= 0:
        raise ApiError("pullRequestId must be a positive integer", 400)
    return number


@router.post("/pr-comment")
async def pr_comment(request: Request) -> dict[str, Any]:
    """Post one top-level comment on a PR. The only write in this API: the provider
    call is **not retried** (a retry could post twice), and the token needs write scope
    (GitHub ``repo``/``pull_requests:write``, GitLab ``api``, Bitbucket
    ``pullrequest:write``, Azure ``Code (read & write)``)."""
    deps: ServerDeps = request.app.state.deps
    settings: Settings = request.app.state.settings
    body = await _body(request)
    _require(body, "repoUrl", "body")
    number = _pr_id(body)
    text = str(body["body"])
    posted = await run_in_threadpool(
        _with_client, deps, settings, body, lambda c, ref: c.post_pr_comment(ref, number, text)
    )
    return {"id": posted.id, "url": posted.url}


def _pr_dict(pr: Any) -> dict[str, Any]:
    return {
        "number": pr.number,
        "title": pr.title,
        "state": pr.state,
        "baseBranch": pr.base_branch,
        "headBranch": pr.head_branch,
        "baseCommitId": pr.base_sha,
        "headCommitId": pr.head_sha,
        "url": pr.url,
    }


@router.post("/check-update")
async def check_update(request: Request) -> dict[str, Any]:
    """Is the stored commit behind the branch tip, and by how many files?

    Mirrors the backend's ``checkUpdate``: fetch the tip, compare its SHA with
    ``currentCommitId``, and only when they differ run a compare. With no stored commit
    (``currentCommitId`` absent or a sentinel) there is nothing to diff against, so
    ``change`` is ``true`` with zero counts — the backend treats that as "needs a first
    analysis", exactly as it did when ``getDiff`` ran without a base.
    """
    deps: ServerDeps = request.app.state.deps
    settings: Settings = request.app.state.settings
    body = await _body(request)
    _require(body, "repoUrl", "gitBranch")
    current = body.get("currentCommitId")
    current = None if current in _NO_COMMIT else str(current)

    def work(client: AbstractSCMClient, ref: RepoRef) -> dict[str, Any]:
        tip = client.branch_head(ref, str(body["gitBranch"]))
        change = tip.sha != current
        changed: list[str] = []
        deleted: list[str] = []
        if change and current:
            diff = client.compare(ref, current, tip.sha)
            changed, deleted = diff.changed, diff.deleted
        return {
            "change": change,
            "latestCommitId": tip.sha,
            "currentCommitId": current,
            "fileUpdatedCount": len(changed),
            "fileDeletedCount": len(deleted),
            "latestCommit": asdict(tip),
            "changedFiles": changed,
            "deletedFiles": deleted,
        }

    return await run_in_threadpool(_with_client, deps, settings, body, work)
