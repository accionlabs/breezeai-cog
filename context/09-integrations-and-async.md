---
title: "breezeai-cog — Integrations and Async"
date: 2026-10-30
---

Scope: Backend upload, SCM provider integrations, object storage, and background tasks.

## Backend upload (httpx)

Source: `src/breezeai_cog/services/upload.py`, `src/breezeai_cog/services/batch_upload.py`

- HTTP client: `httpx` (core dependency).
- `upload_ontology` (`upload.py:33`): `POST <baseurl>/code-ontology/generate`, multipart form with `file` (`application/gzip`), `projectUuid` and `name` (repository name).
- Auth: `api-key: <user_api_key>` header.
- Timeout per request: `upload_timeout` (default 900 s). Attempts: `upload_max_retries + 1`; network errors and HTTP 5xx retry with a linear sleep, 4xx is fatal (`UploadError`).
- `extract_ontology_id` (`upload.py:95`) reads `_id` from the response; `poll_ontology_status` (`upload.py:124`) polls `GET <baseurl>/code-ontology` until `fileGraphStatus` is `active` or `error` (60 s interval, bounded by `upload_timeout` in batch mode).
- `run_batch_uploads` (`batch_upload.py:192`) runs tasks on up to `upload_parallelism` threads and marks a repo done only after its poll returns `active`.
- Batch resume: `UploadState` persisted in `<workspace>/.cog/batch-upload-state.json`; cleared once every task succeeds.

## Backend notification

Source: `src/breezeai_cog/services/notify.py`

`post_notification` (`notify.py:40`) POSTs JSON to `<baseurl><path>` with the `api-key` header. Routes schedule it as a FastAPI `BackgroundTask` after the response is sent, so a failure is logged only. Retries: `NOTIFY_ATTEMPTS = 3` on connect errors and HTTP 500/502/503 (backoff 1 s, 3 s); a read timeout, 504 and 4xx are not retried.

| Route | Endpoint | Payload |
|---|---|---|
| `/api/analyze-diff` | `/code-ontology/stream-ingest` | `{storage_key, projectMetaData, deletedFiles, projectUuid, codeOntologyId, repoUrl, gitBranch, commitId}` |
| `/api/analyze-sql`, `-nosql`, `-es` | `/db-ontology/stream-ingest-s3` | `{storage_key, projectUuid, dataLakeId}` |

`storage_key` and `commitId` are built in `src/breezeai_cog/server/routes.py` (`analyze_diff`, line 98), not read from a model.

## Object storage (server)

The provider contract, upload lifecycle, retry boundaries, and extension instructions are maintained
in [`docs/DEVELOPER_GUIDE.md`](../docs/DEVELOPER_GUIDE.md#object-storage-providers). Route-specific
storage keys and settings are described in this context's configuration and service references;
verify them against `src/breezeai_cog/infra/` before use.

## SCM provider integrations

Provider selection, the SCM client contract, retry rules, error mapping, and adding a provider are
covered by [`docs/DEVELOPER_GUIDE.md`](../docs/DEVELOPER_GUIDE.md#git-providers-apianalyze-diff).
For current behavior, inspect `src/breezeai_cog/integrations/scm/` and
`src/breezeai_cog/server/git.py`.

## Background tasks (FastAPI)

Notification after upload is a `BackgroundTask` that runs after the HTTP response is sent. If it fails, the caller (backend) is not told; the log carries the error. The route's primary work (parse and upload to storage) is already done by then.

