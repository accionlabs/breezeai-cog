---
title: "breezeai-cog — Integrations and Async"
date: 2026-10-30
---

Scope: Backend upload, SCM provider integrations, S3 infra, background tasks, Node 22 requirement.

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

Source: `src/breezeai_cog/infra/`

- `InfraStream` (`infra/interface.py:4`): abstract streaming upload with `write_line(line)` and `close() -> key`; `close` raises if the object is incomplete.
- `open_stream(key, settings)` (`infra/provider.py:7`) picks the provider through `ProviderConfig.from_settings` and `ProviderFactory.create_stream`; `ProviderType` has only `AWS`.
- `AWSStreamUpload` (`infra/aws/s3.py:60`): gzips lines into a pipe read by a background thread running boto3 `upload_fileobj` (multipart).
- `ServerDeps.open_storage(key)` (`server/deps.py:16`) wraps `open_stream`.

Bucket: `Settings.aws_s3_bucket`, env `AWS_S3_BUCKET`. Region `AWS_REGION` (default `us-west-2`); keys `AWS_ACCESS_KEY` / `AWS_SECRET_KEY`. Provider: `infra_provider` (only `aws`). Key patterns:
- Code ontology: `code-ontology/<projectUuid>/<incomingCommitId>.ndjson.gz`
- DDL: `db-ontology/<projectUuid>/<dataLakeId>/<timestamp-ms>-<name>.ndjson.gz`; NoSQL: `nosql-ontology/...`; ES: `es-ontology/...` (settings-only adds a `-settings` suffix)

Shared boto3 client config: `storage_retry_attempts` (botocore retries, adaptive mode), `storage_connect_timeout` (10 s), `storage_read_timeout` (60 s).

## SCM provider integrations

Source: `src/breezeai_cog/integrations/scm/`

Providers: `GitHubSCMClient` (`github.py:29`), `GitLabSCMClient` (`gitlab.py:25`), `BitbucketSCMClient` (`bitbucket.py:36`), `AzureDevOpsSCMClient` (`azure_devops.py:60`), all subclasses of `AbstractSCMClient` (`base.py:205`). `SUPPORTED_PROVIDERS = {"github", "bitbucket", "gitlab", "azure_devops"}` in `base.py`.

### Provider selection

1. `parse_repo_url(url, instances)` in `repository.py:122` returns `RepoRef(provider, owner, repo, project, host)` or `None`. Public-cloud hosts match by pattern; `scm_instances` (`host -> provider slug`) adds self-hosted hosts.
2. `SCMClientFactory.for_repo(ref, settings, request_token)` (`factory.py:118`) resolves the token (`resolve_scm_token`: request `gitToken`, else `scm_token_<provider>`) and the API base (`resolve_api_base_url`: `scm_instance_base_url_mapping` for self-hosted hosts), then `SCMClientFactory.create` instantiates the class.

### AbstractSCMClient interface

Source: `src/breezeai_cog/integrations/scm/base.py:205`

| Method | Description |
|---|---|
| `tree(ref, commit)` | Every blob path at a commit |
| `branch_head(ref, branch)` | Branch tip as `CommitInfo(sha, message, author, date)` |
| `pull_request(ref, number)` | `PullRequestInfo` (state normalised to open/merged/closed) |
| `post_pr_comment(ref, number, body)` | Post a top-level comment (the only write; not retried) |
| `compare(ref, base, head)` | `ChangeSet(changed, deleted, files)` |
| `file_content(ref, path, commit)` | Decoded text of one file at a commit |
| `clone_url(ref)` | HTTPS clone URL with the credential embedded |
| `close()` | Release pooled connections; clients are context managers |

HTTP layer: `SCMHttpClient` (`http.py:45`) wraps `httpx.Client`; `request_with_retry` (`retry.py:54`) retries 429/502/503/504 and transport errors with exponential backoff and jitter, capped at 60 s. Settings: `scm_api_timeout`, `scm_api_retry_max`, `scm_api_retry_backoff_seconds`, `scm_max_pages`.

Errors: `SCMError` (`errors.py:12`) carries `status_code` (502 for `SCMAPIError`, 400 for `SCMCredentialError` and `UnsupportedSCMProviderError`).

### Diff acquisition (analyze-diff)

Source: `src/breezeai_cog/server/git.py`, `server/deps.py`

`acquire_diff(settings, body)` (`git.py:153`), exposed as `ServerDeps.acquire_diff`:
1. Builds the client for `repoUrl` with `gitToken` from the body (else the global token).
2. With a usable `currentCommitId` and `client.supports_incremental`, `resolve_git_diff` (`git.py:96`) calls `compare`, writes every `tree` path as an empty file in a temp dir and real content for changed files; returns `(temp_dir, filter_set, deleted)`.
3. Otherwise `clone_repo_full` (`git.py:52`) shallow-clones the branch, checks out `incomingCommitId`, drops `.git` and returns `filter_set=None`; `git_clone_timeout` (1800 s) caps each git call.

Credentials are scrubbed from error text by `_scrub` (`git.py:48`).

## Background tasks (FastAPI)

Notification after upload is a `BackgroundTask` that runs after the HTTP response is sent. If it fails, the caller (backend) is not told; the log carries the error. The route's primary work (parse and upload to storage) is already done by then.

## Node 22 requirement

When `breezeai-cog` is driven by the `breeze:onboard-repository` skill (which shells out via Node), **Node 22 is required**. Node 24 fails silently on tree-sitter native bindings (the bindings load but produce no output). This is a host-environment constraint, not a Python dependency.
