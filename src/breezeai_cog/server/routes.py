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
from ..analyzers.sql import parse_ddl
from ..services.diff import empty_meta, run_diff_stream
from ..services.inprocess import analyze_in_memory
from .deps import ServerDeps
from .errors import ApiError
from .git import parse_repo_url

router = APIRouter()

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_name(name: str) -> str:
    return _SAFE.sub("_", name)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _stream_records_to_s3(deps: ServerDeps, key: str, records: list[dict]) -> str:
    stream = deps.open_s3(key)
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
        raise ApiError("Invalid repo URL (supported hosts: github.com, bitbucket.org)", 400)
    repo_name = parsed["repo"]

    temp_dir, filter_set, deleted_files = await run_in_threadpool(deps.acquire_diff, settings, body)
    s3_key = f"code-ontology/{project_uuid}/{incoming}.ndjson.gz"
    has_changed = filter_set is None or len(filter_set) > 0
    try:
        if has_changed:
            upload = deps.open_s3(s3_key)
            meta = await run_in_threadpool(run_diff_stream, settings, upload, temp_dir, filter_set, repo_name)
        else:
            upload = deps.open_s3(s3_key)
            await run_in_threadpool(upload.close)
            meta = empty_meta(repo_name)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    meta["repoUrl"] = repo_url
    meta["gitBranch"] = git_branch
    meta["commitId"] = incoming

    background_tasks.add_task(
        deps.notify, "/code-ontology/stream-ingest",
        {"s3Key": s3_key, "projectMetaData": meta, "deletedFiles": deleted_files,
         "projectUuid": project_uuid, "codeOntologyId": code_ontology_id,
         "repoUrl": repo_url, "gitBranch": git_branch, "commitId": incoming},
    )

    return {
        "success": True,
        "s3Key": s3_key,
        "deletedFiles": deleted_files,
        "message": (
            "Code ontology streamed to S3 and notification sent to Breeze API for ingestion."
            if has_changed else
            "Deletion-only commit — notification sent to Breeze API with deleted files."
        ),
    }


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

    s3_key = f"db-ontology/{projectUuid}/{dataLakeId}/{_now_ms()}-{_safe_name(file_name)}.ndjson.gz"
    await run_in_threadpool(_stream_records_to_s3, deps, s3_key, [record])
    background_tasks.add_task(
        deps.notify, "/db-ontology/stream-ingest-s3",
        {"s3Key": s3_key, "projectUuid": projectUuid, "dataLakeId": dataLakeId,
         "repositoryName": repositoryName or file_name},
    )

    return JSONResponse(status_code=202, content={
        "success": True,
        "s3Key": s3_key,
        "fileName": file_name,
        "dialect": parsed["dialect"],
        "tableCount": len(parsed["tables"]),
        "viewCount": len(parsed["views"]),
        "procedureCount": len(parsed["procedures"]),
        "indexCount": len(parsed["allIndexes"]),
        "sequenceCount": len(parsed["sequences"]),
        "message": "SQL parsed, NDJSON.gz streamed to S3, ingestion notification sent.",
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
    s3_key = f"es-ontology/{projectUuid}/{dataLakeId}/{_now_ms()}-{_safe_name(primary_name)}{suffix}.ndjson.gz"

    await run_in_threadpool(_stream_records_to_s3, deps, s3_key, build["records"])
    background_tasks.add_task(
        deps.notify, "/db-ontology/stream-ingest-s3",
        {"s3Key": s3_key, "projectUuid": projectUuid, "dataLakeId": dataLakeId,
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
        "s3Key": s3_key,
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
