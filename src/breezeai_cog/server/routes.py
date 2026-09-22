"""FastAPI routes — mirror the JS ``server.js`` contract exactly. Body validation
is manual (not pydantic) so error messages match byte-for-byte. CPU-bound work and the
blocking S3 upload run off the event loop; backend notifications are fire-and-forget
(BackgroundTasks). ``llmPlatform`` is never read or forwarded (accepted deviation)."""

from __future__ import annotations

import json
import re
import shutil
import time

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ..analyzers.es import BuildError, build_es_records
from ..analyzers.nosql import BuildError as NoSqlBuildError
from ..analyzers.nosql import build_nosql_records
from ..analyzers.sql import parse_ddl
from ..core.ignore import append_repo_ignore_patterns
from ..services.diff import empty_meta, run_diff_stream
from ..services.inprocess import analyze_in_memory
from .deps import ServerDeps
from .errors import ApiError
from .git import (
    fetch_commit,
    fetch_directory_tree,
    fetch_git_diff,
    fetch_latest_commit,
    fetch_pull_request,
    parse_repo_url,
    post_commit_comment,
    post_pull_request_comment,
    update_commit_status,
)

router = APIRouter()

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_name(name: str) -> str:
    return _SAFE.sub("_", name)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _normalize_ignore_patterns(raw: object) -> list[str]:
    """``ignorePatterns`` (optional) — a newline-separated string or a list of strings.

    Absent/blank means "no extra patterns": the scan then behaves exactly as before,
    honouring only the built-in defaults and the repo's own ignore files.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [ln for ln in (line.strip() for line in raw.splitlines()) if ln]
    if isinstance(raw, list):
        out: list[str] = []
        for i, item in enumerate(raw):
            if not isinstance(item, str):
                raise ApiError(f"ignorePatterns[{i}] must be a string", 400)
            if item.strip():
                out.append(item.strip())
        return out
    raise ApiError("ignorePatterns must be a string or an array of strings", 400)


def _stream_records_to_infra(deps: ServerDeps, key: str, records: list[dict]) -> str:
    stream = deps.open_storage(key)
    for record in records:
        stream.write_line(json.dumps(record) + "\n")
    return stream.close()

@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/api/analyze")
async def analyze(request: Request) -> dict:
    settings = request.app.state.settings
    body = await request.json()
    files = body.get("files")
    project_name = body.get("projectName")

    if not isinstance(files, list) or len(files) == 0:
        raise ApiError('"files" must be a non-empty array', 400)
    for i, f in enumerate(files):
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) or not isinstance(f.get("content"), str):
            raise ApiError(f'files[{i}] must have "path" (string) and "content" (string)', 400)
        if ".." in f["path"]:
            raise ApiError(f'files[{i}].path must not contain ".."', 400)

    output = await run_in_threadpool(analyze_in_memory, settings, files, project_name)
    if not output["files"]:
        raise ApiError("No supported languages detected in the provided files", 422)
    return output


@router.post("/api/analyze-diff")
async def analyze_diff(request: Request, background_tasks: BackgroundTasks) -> dict:
    deps: ServerDeps = request.app.state.deps
    settings = request.app.state.settings
    body = await request.json()

    repo_url = body.get("repoUrl")
    incoming = body.get("incomingCommitId")
    git_branch = body.get("gitBranch")
    project_uuid = body.get("projectUuid")
    code_ontology_id = body.get("codeOntologyId")
    if not (repo_url and incoming and git_branch and project_uuid and code_ontology_id):
        raise ApiError(
            "All fields required: repoUrl, incomingCommitId, gitBranch, projectUuid, codeOntologyId", 400
        )
    parsed = parse_repo_url(repo_url)
    if parsed is None:
        raise ApiError("Invalid repo URL (supported hosts: github.com, bitbucket.org, gitlab.com, dev.azure.com)", 400)
    repo_name = parsed["repo"]
    ignore_patterns = _normalize_ignore_patterns(body.get("ignorePatterns"))

    temp_dir, filter_set, deleted_files = await run_in_threadpool(deps.acquire_diff, settings, body)
    storage_key= f"code-ontology/{project_uuid}/{incoming}.ndjson.gz"
    has_changed = filter_set is None or len(filter_set) > 0
    try:
        if ignore_patterns:
            # Fail loudly: a silent write failure would ship files the caller
            # explicitly asked to exclude into the ontology.
            try:
                append_repo_ignore_patterns(temp_dir, ignore_patterns)
            except OSError as exc:
                raise ApiError(f"Failed to apply ignorePatterns: {exc}", 500) from exc
        if has_changed:
            upload = deps.open_storage(storage_key)
            meta = await run_in_threadpool(run_diff_stream, settings, upload, temp_dir, filter_set, repo_name)
        else:
            upload = deps.open_storage(storage_key)
            await run_in_threadpool(upload.close)
            meta = empty_meta(repo_name)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    meta["repoUrl"] = repo_url
    meta["gitBranch"] = git_branch
    meta["commitId"] = incoming

    background_tasks.add_task(
        deps.notify, "/code-ontology/stream-ingest",
        {"s3Key": storage_key, "projectMetaData": meta, "deletedFiles": deleted_files,
         "projectUuid": project_uuid, "codeOntologyId": code_ontology_id,
         "repoUrl": repo_url, "gitBranch": git_branch, "commitId": incoming},
    )

    if has_changed:
        message = "Code ontology streamed to S3 and notification sent to Breeze API for ingestion."
    elif deleted_files:
        message = "Deletion-only commit — notification sent to Breeze API with deleted files."
    else:
        message = "No file changes between commits — commit recorded, ontology left unchanged."

    return {
        "success": True,
        "storage_key": storage_key,
        "deletedFiles": deleted_files,
        "message": message,
    }


@router.post("/api/git-diff")
async def git_diff(request: Request) -> dict:
    """Provider-agnostic file-level diff between two commits (BREEZEAI-1228).

    The single place any Breeze service asks a git host "what changed?". Replaces
    the four private `get*Diff` helpers in BreezeAI_Backend's `GitService`, the
    `scm/` diff paths in sdlc-autonomous-agents, and the N8N `Get_PR_Diff_*` nodes.

    Body — `repoUrl` and `incomingCommitId` are required; everything else is
    optional. `gitBranch`, `projectUuid` and `codeOntologyId` are ACCEPTED for
    payload symmetry with `/api/analyze-diff` but are not needed to compute a
    diff and are not read (same accepted deviation as `llmPlatform`).

        repoUrl          str   required — provider inferred from the host
        incomingCommitId str   required — head commit SHA
        baseCommitId     str?  omit/null for a single-commit diff
        gitToken         str?  provider credential; omit for a public repo
        gitBranch        str?  accepted, unused
        projectUuid      str?  accepted, unused
        codeOntologyId   any?  accepted, unused

    Returns `{baseSha, headSha, totalFiles, files[]}`. `files[].additions`,
    `.deletions` and `.patch` are **nullable** — `null` means "this provider does
    not report it", which `0` could not distinguish from a genuine zero. See the
    provider capability table in `server/git.py`.
    """
    body = await request.json()

    repo_url = body.get("repoUrl")
    incoming = body.get("incomingCommitId")
    if not repo_url or not incoming:
        raise ApiError("All fields required: repoUrl, incomingCommitId", 400)

    base = body.get("baseCommitId")
    # The backend sends JSON null, but a stringified "null"/"undefined" has
    # historically reached these endpoints too — treat every one as "no base".
    if base in (None, "", "null", "undefined"):
        base = None

    # Blocking httpx calls — keep them off the event loop.
    return await run_in_threadpool(fetch_git_diff, repo_url, incoming, base, body.get("gitToken"))


@router.post("/api/pull-request")
async def pull_request(request: Request) -> dict:
    """Provider-agnostic pull-request lookup (BREEZEAI-1228 follow-up).

    Replaces `GitService.getPullRequestInfo` AND `getPullRequestBaseBranch` in
    BreezeAI_Backend. Both are the same call: the "lightweight" variant is just
    `includeCommits: false`, since skipping the paginated commit listing was the
    only thing that made it cheap.

    Body:
        repoUrl         str      required — provider inferred from the host
        pullRequestId   str|int  required — PR / MR / pullRequest id
        gitToken        str?     provider credential; omit for a public repo
        includeCommits  bool?    default true; false skips the commits fetch

    Returns `{id, title, state, baseBranch, sourceBranch, baseCommitSha,
    headCommitSha, commits[]}`. `commits` is `[]` when `includeCommits` is false
    — NOT an indication that the PR has no commits.

    "Pull request" is canonical: GitLab merge requests and Azure pullRequests are
    translated internally, so the caller never branches on provider.
    """
    body = await request.json()

    repo_url = body.get("repoUrl")
    pr_id = body.get("pullRequestId")
    # 0 is not a valid PR id on any provider, so a plain falsy check is safe here.
    if not repo_url or not pr_id:
        raise ApiError("All fields required: repoUrl, pullRequestId", 400)

    include_commits = body.get("includeCommits")
    include_commits = True if include_commits is None else bool(include_commits)

    return await run_in_threadpool(
        fetch_pull_request, repo_url, pr_id, body.get("gitToken"), include_commits
    )


@router.post("/api/latest-commit")
async def latest_commit(request: Request) -> dict:
    """Tip commit of a branch (BREEZEAI-1228 follow-up).

    Replaces `GitService.getLatestCommit` in BreezeAI_Backend.

    Body:
        repoUrl    str   required — provider inferred from the host
        gitBranch  str?  defaults to "main", as the backend did
        gitToken   str?  provider credential; omit for a public repo

    Returns `{sha, message, author, date}`. 404 when the branch does not exist
    (Azure reports this explicitly; the others surface it as a provider 404).
    """
    body = await request.json()

    repo_url = body.get("repoUrl")
    if not repo_url:
        raise ApiError("All fields required: repoUrl", 400)

    branch = body.get("gitBranch") or "main"
    return await run_in_threadpool(fetch_latest_commit, repo_url, branch, body.get("gitToken"))


@router.post("/api/pr-comment")
async def pr_comment(request: Request) -> dict:
    """Post a comment on a pull request (BREEZEAI-1228 follow-up).

    Replaces `GitService.postPrComment`. ⚠️ It takes `repoUrl` + `pullRequestId`
    rather than the backend's pre-built `commentUrl`: COG builds the provider URL
    itself, so this endpoint cannot be used to POST to an arbitrary address, and
    Azure gets its real threads API instead of a GitHub-shaped payload sent to
    the wrong URL. See `post_pull_request_comment` in `server/git.py`.

    Body:
        repoUrl        str      required
        pullRequestId  str|int  required
        body           str      required — the comment text (Markdown on most providers)
        gitToken       str?     omit only if the repo accepts anonymous comments (none do)

    Returns `{posted: true, provider: "<provider>"}`.
    """
    payload = await request.json()

    repo_url = payload.get("repoUrl")
    pr_id = payload.get("pullRequestId")
    comment = payload.get("body")
    if not repo_url or not pr_id or not comment:
        raise ApiError("All fields required: repoUrl, pullRequestId, body", 400)

    return await run_in_threadpool(
        post_pull_request_comment, repo_url, pr_id, comment, payload.get("gitToken")
    )


@router.post("/api/directory-tree")
async def directory_tree(request: Request) -> dict:
    """Recursive file listing for a branch (BREEZEAI-1228 follow-up).

    Replaces `GitService.getDirectoryTree` in BreezeAI_Backend.

    Body:
        repoUrl    str   required — provider inferred from the host
        gitBranch  str?  defaults to "main"
        gitToken   str?  provider credential; omit for a public repo

    Returns `{entries: [{path, type, size}], truncated}`. `type` is "tree" or
    "blob"; `size` is null where the provider does not report it. `truncated` is
    true only on GitHub, whose tree endpoint caps its response rather than
    paginating — treat the listing as incomplete when you see it.
    """
    body = await request.json()

    repo_url = body.get("repoUrl")
    if not repo_url:
        raise ApiError("All fields required: repoUrl", 400)

    branch = body.get("gitBranch") or "main"
    return await run_in_threadpool(fetch_directory_tree, repo_url, branch, body.get("gitToken"))


@router.post("/api/commit")
async def commit(request: Request) -> dict:
    """A single commit with its file changes (BREEZEAI-1228 follow-up).

    Covers `AbstractSCMClient.get_commit` for sdlc-autonomous-agents.

    Body: `repoUrl` + `sha` required; `gitToken` optional.

    Returns `{sha, message, author, authoredAt, url, branch, files[], truncated}`.
    `truncated` is true when the provider capped the file list — GitHub's
    single-commit endpoint stops at 300 files and cannot be paginated, so a
    consumer that ignores this flag will act on partial data.

    `jiraTicketKeys` is deliberately NOT returned: callers extract those from
    `message` with their own configured pattern.
    """
    body = await request.json()
    repo_url, sha = body.get("repoUrl"), body.get("sha")
    if not repo_url or not sha:
        raise ApiError("All fields required: repoUrl, sha", 400)
    return await run_in_threadpool(fetch_commit, repo_url, sha, body.get("gitToken"))


@router.post("/api/commit-comment")
async def commit_comment(request: Request) -> dict:
    """Comment on a commit (BREEZEAI-1228 follow-up).

    Covers `AbstractSCMClient.post_commit_comment`.

    Body: `repoUrl`, `sha`, `body` required; `gitToken` optional.

    Returns `{posted, provider}`. ⚠️ **Azure DevOps has no commit-comment API**, so
    it returns `posted: false` with a `reason` instead of raising. Treat
    `posted: false` as a real outcome — do not report a comment that does not exist.
    """
    payload = await request.json()
    repo_url, sha, text = payload.get("repoUrl"), payload.get("sha"), payload.get("body")
    if not repo_url or not sha or not text:
        raise ApiError("All fields required: repoUrl, sha, body", 400)
    return await run_in_threadpool(post_commit_comment, repo_url, sha, text, payload.get("gitToken"))


@router.post("/api/commit-status")
async def commit_status(request: Request) -> dict:
    """Post or update a commit status check (BREEZEAI-1228 follow-up).

    Covers `AbstractSCMClient.update_commit_status` — the call that gates a PR.

    Body:
        repoUrl      str  required
        sha          str  required
        state        str  required — pending | success | failure | error
        description  str? truncated per provider (GitHub 140, GitLab 250,
                          Bitbucket 255, Azure 4000)
        context      str? status name; Azure splits it on "/" into genre/name
        targetUrl    str? link shown next to the check
        buildKey     str? REQUIRED for Bitbucket, ignored elsewhere
        gitToken     str?

    Returns `{posted, provider, state}` with the provider's own spelling of the
    state. Bitbucket without `buildKey` returns `posted: false` and a reason.
    """
    payload = await request.json()
    repo_url, sha, state = payload.get("repoUrl"), payload.get("sha"), payload.get("state")
    if not repo_url or not sha or not state:
        raise ApiError("All fields required: repoUrl, sha, state", 400)
    return await run_in_threadpool(
        update_commit_status, repo_url, sha, state, payload.get("description") or "",
        payload.get("context") or "breezeai", payload.get("targetUrl") or "",
        payload.get("gitToken"), payload.get("buildKey"),
    )


@router.post("/api/analyze-sql")
async def analyze_sql(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(None),
    projectUuid: str = Form(None),
    dataLakeId: str = Form(None),
    repositoryName: str = Form(None),
) -> JSONResponse:
    deps: ServerDeps = request.app.state.deps

    if file is None:
        raise ApiError("Multipart 'file' field is required", 400)
    if not projectUuid:
        raise ApiError("projectUuid is required", 400)
    if not dataLakeId:
        raise ApiError("dataLakeId is required", 400)

    file_name = file.filename or "uploaded.sql"
    ddl_text = (await file.read()).decode("utf-8")
    parsed = parse_ddl(ddl_text, file_name)

    if not (parsed["tables"] or parsed["views"] or parsed["procedures"]
            or parsed["allIndexes"] or parsed["sequences"]):
        body = {"error": "No DDL objects could be extracted from the SQL file", "dialect": parsed["dialect"]}
        if parsed.get("parseReport"):
            body["parseReport"] = parsed["parseReport"]
        return JSONResponse(status_code=422, content=body)

    record = {
        "__type": "ddl",
        "path": file_name,
        "language": "sql",
        "dialect": parsed["dialect"],
        "tables": parsed["tables"],
        "views": parsed["views"],
        "procedures": parsed["procedures"],
        "indexes": parsed["allIndexes"],
        "sequences": parsed["sequences"],
    }
    if parsed.get("parseReport"):
        record["parseReport"] = parsed["parseReport"]

    storage_key = f"db-ontology/{projectUuid}/{dataLakeId}/{_now_ms()}-{_safe_name(file_name)}.ndjson.gz"
    await run_in_threadpool(_stream_records_to_infra, deps, storage_key, [record])
    background_tasks.add_task(
        deps.notify, "/db-ontology/stream-ingest-s3",
        {"s3Key": storage_key, "projectUuid": projectUuid, "dataLakeId": dataLakeId,
         "repositoryName": repositoryName or file_name},
    )

    return JSONResponse(status_code=202, content={
        "success": True,
        "storage_key":storage_key,
        "fileName": file_name,
        "dialect": parsed["dialect"],
        "tableCount": len(parsed["tables"]),
        "viewCount": len(parsed["views"]),
        "procedureCount": len(parsed["procedures"]),
        "indexCount": len(parsed["allIndexes"]),
        "sequenceCount": len(parsed["sequences"]),
        "message": "SQL parsed, NDJSON.gz streamed to S3, ingestion notification sent.",
    })


@router.post("/api/analyze-nosql")
async def analyze_nosql(
    request: Request,
    background_tasks: BackgroundTasks,
    file: list[UploadFile] = File(None),
    projectUuid: str = Form(None),
    dataLakeId: str = Form(None),
    repositoryName: str = Form(None),
) -> JSONResponse:
    deps: ServerDeps = request.app.state.deps

    if not file:
        raise ApiError("At least one multipart 'file' is required", 400)
    if not projectUuid:
        raise ApiError("projectUuid is required", 400)
    if not dataLakeId:
        raise ApiError("dataLakeId is required", 400)

    uploads = []
    for f in file:
        data = await f.read()
        uploads.append({"name": f.filename or "uploaded.json", "text": data.decode("utf-8"), "size": len(data)})

    try:
        build = build_nosql_records(uploads)
    except NoSqlBuildError as exc:
        raise ApiError(str(exc), exc.status_code)

    primary_name = build["collections"][0]
    storage_key = f"nosql-ontology/{projectUuid}/{dataLakeId}/{_now_ms()}-{_safe_name(primary_name)}.ndjson.gz"

    await run_in_threadpool(_stream_records_to_infra, deps, storage_key, build["records"])
    background_tasks.add_task(
        deps.notify, "/db-ontology/stream-ingest-s3",
        {"s3Key": storage_key, "projectUuid": projectUuid, "dataLakeId": dataLakeId,
         "repositoryName": repositoryName or primary_name},
    )

    n = len(uploads)
    cc = build["collectionCount"]
    message = (
        f"NoSQL schema ({n} file{'' if n == 1 else 's'}, "
        f"{cc} collection{'' if cc == 1 else 's'}) parsed; "
        "NDJSON.gz streamed to S3 and ingestion notification sent."
    )
    return JSONResponse(status_code=202, content={
        "success": True,
        "storage_key":storage_key,
        "collections": build["collections"],
        "recordCount": len(build["records"]),
        "collectionCount": cc,
        "fieldCount": build["fieldCount"],
        "indexCount": build["indexCount"],
        "message": message,
    })


@router.post("/api/analyze-es")
async def analyze_es(
    request: Request,
    background_tasks: BackgroundTasks,
    file: list[UploadFile] = File(None),
    projectUuid: str = Form(None),
    dataLakeId: str = Form(None),
    repositoryName: str = Form(None),
) -> JSONResponse:
    deps: ServerDeps = request.app.state.deps

    if not file:
        raise ApiError("At least one multipart 'file' is required", 400)
    if not projectUuid:
        raise ApiError("projectUuid is required", 400)
    if not dataLakeId:
        raise ApiError("dataLakeId is required", 400)

    uploads = []
    for f in file:
        data = await f.read()
        uploads.append({"name": f.filename or "uploaded.json", "text": data.decode("utf-8"), "size": len(data)})

    try:
        build = build_es_records(uploads)
    except BuildError as exc:
        raise ApiError(str(exc), exc.status_code)

    primary_name = (build["mapping"] or build["setting"])["name"]
    suffix = "-settings" if build["kind"] == "settings-only" else ""
    storage_key = f"es-ontology/{projectUuid}/{dataLakeId}/{_now_ms()}-{_safe_name(primary_name)}{suffix}.ndjson.gz"

    await run_in_threadpool(_stream_records_to_infra, deps, storage_key, build["records"])
    background_tasks.add_task(
        deps.notify, "/db-ontology/stream-ingest-s3",
        {"s3Key": storage_key, "projectUuid": projectUuid, "dataLakeId": dataLakeId,
         "repositoryName": repositoryName or primary_name},
    )

    n_map, n_set = len(build["mappings"]), len(build["settings"])
    if build["kind"] == "mapping":
        message = (
            f"ES mapping ({n_map} file{'' if n_map == 1 else 's'}, "
            f"{build['indexCount']} index{'' if build['indexCount'] == 1 else 'es'}) parsed; "
            "NDJSON.gz streamed to S3 and ingestion notification sent."
        )
    else:
        message = (
            f"ES settings ({n_set} file{'' if n_set == 1 else 's'}) parsed; "
            "NDJSON.gz streamed to S3 and settings-patch notification sent."
        )

    return JSONResponse(status_code=202, content={
        "success": True,
        "storage_key": storage_key,
        "mode": build["kind"],
        "mapping": build["mapping"]["name"] if build["mapping"] else None,
        "setting": build["setting"]["name"] if build["setting"] else None,
        "mappings": [m["name"] for m in build["mappings"]],
        "settings": [s["name"] for s in build["settings"]],
        "recordCount": len(build["records"]),
        "indexCount": build["indexCount"],
        "fieldCount": build["fieldCount"],
        "settingsMatched": build["settingsMatched"],
        "message": message,
    })
