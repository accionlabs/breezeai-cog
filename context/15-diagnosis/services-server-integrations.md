---
title: "breezeai-cog — Diagnosis: Services, Server, and Integrations"
date: 2026-10-30
---

Scope: Failure modes, assumptions, blast radius, and fixes for analysis service, upload, git server, notification, and S3 storage.

## Git clone or checkout failure

- **Symptom:** `/api/analyze-diff` returns `{"error": "Git acquisition failed: ...", "failedStep": "git_acquire"}`. Status 502 for provider or clone failures (`RuntimeError`, `httpx.HTTPError`), 500 for an unexpected local error, or the status of an `SCMError` (400 for a bad credential or unsupported host).
- **Invariant / Assumption:** The provider token can read the repository, and the `git` binary is on `PATH` for a full clone. Clone URLs and error text are scrubbed of credentials by `_scrub`.
- **Origin site:** `src/breezeai_cog/server/git.py:52` (`clone_repo_full`), `src/breezeai_cog/server/git.py:153` (`acquire_diff`) and `src/breezeai_cog/server/routes.py:133`.
- **Blast radius:** The request aborts before parsing; the temp checkout directory is removed.
- **Fix approach:**
  1. Check repository access and token scope (GitLab needs `read_api`; a 502 "none could be read" means changed files were unreadable).
  2. Verify `git` is installed and reachable.
  3. For self-hosted hosts, set `scm_instances` and `scm_instance_base_url_mapping`; a clone slower than `git_clone_timeout` (1800 s) fails.

## Backend upload failure (CLI)

- **Symptom:** `UploadError: upload failed for <file>: <error>`, or a config error such as `baseurl is not configured`.
- **Invariant / Assumption:** `--upload` needs `baseurl` (`BREEZE_API_URL`), `uuid` and `user_api_key` (`API_KEY`), and the artifact file must exist. 4xx responses are fatal; network errors and 5xx retry `upload_max_retries` times.
- **Origin site:** `src/breezeai_cog/services/upload.py:33` (`upload_ontology`, config checks at lines 54-62, final raise at line 92).
- **Blast radius:** The capture exists locally, but the backend graph receives nothing and the repo is not marked done in the batch resume state.
- **Fix approach:**
  1. Confirm `--baseurl` / `BREEZE_API_URL` reaches the backend.
  2. Confirm `--user-api-key` / `API_KEY` and `--uuid` are valid.
  3. If the backend never reports `active`, `poll_ontology_status` (`upload.py:124`) fails after `upload_timeout`; raise `--upload-timeout`.

## Storage open / upload failure (server)

- **Symptom:** `{"error": "Storage open failed: ...", "failedStep": "upload"}` or `{"error": "Upload failed: ...", "failedStep": "upload"}` with status 500.
- **Invariant / Assumption:** `AWS_S3_BUCKET` is set and boto3 can reach S3 with the configured credentials; `AWSStreamUpload` raises `ValueError` when the bucket is missing. `close()` raises if the object is incomplete.
- **Origin site:** `src/breezeai_cog/server/routes.py:156` and `src/breezeai_cog/infra/aws/s3.py:60`.
- **Blast radius:** No artifact is stored and no backend notification is sent; the caller gets the 500.
- **Fix approach:** Check `AWS_S3_BUCKET`, `AWS_REGION`, `AWS_ACCESS_KEY` / `AWS_SECRET_KEY` (or the ambient AWS credentials) and the `storage_connect_timeout` / `storage_read_timeout` settings.

## Backend notification failure

- **Symptom:** Warning `"... (attempt X/3, retrying in Ys)"`, ending in `RuntimeError: ... (after 3 attempts)` in the server log. The HTTP response was already sent.
- **Invariant / Assumption:** `post_notification` retries `NOTIFY_ATTEMPTS = 3` times on connect errors and HTTP 500/502/503; it needs `baseurl` and sends the `api-key` header.
- **Origin site:** `src/breezeai_cog/services/notify.py:40`.
- **Blast radius:** Parsing and storage upload succeeded, but the backend never ingests the artifact and its ontology status stays unchanged.
- **Fix approach:** Set `BREEZE_API_URL` and `API_KEY` and check the backend `/code-ontology/stream-ingest` and `/db-ontology/stream-ingest-s3` routes respond.

## Malformed route request payload

- **Symptom:** HTTP 400 `{"error": ...}` (validation errors also map to 400), or 422 when nothing could be extracted.
- **Invariant / Assumption:** `/api/analyze` needs a non-empty `files` array of `{path, content}` with no `..` in `path`, and at least one supported language (else 422). Git routes need their required body fields and a recognised `repoUrl`. SQL/NoSQL/ES routes need `file`, `projectUuid` and `dataLakeId`.
- **Origin site:** `src/breezeai_cog/server/routes.py:84-93` and `src/breezeai_cog/server/git_routes.py:50`.
- **Blast radius:** Request is rejected before git acquisition or parsing starts.
- **Fix approach:** Match the request body to the route in `context/07-api-surface.md`; keep paths relative and files in a supported language.
