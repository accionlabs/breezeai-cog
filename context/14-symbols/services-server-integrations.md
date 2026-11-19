---
title: "breezeai-cog — Symbol Reference: Services, Server, Integrations"
date: 2026-10-30
---

Scope: `services/`, `server/`, `infra/`, `analyzers/`, `integrations/scm/`.

## services/analysis.py

| Symbol | Anchor | Notes |
|---|---|---|
| `AnalysisResult` | `src/breezeai_cog/services/analysis.py:17` | Frozen dataclass `{project_meta: ProjectMetaData, out_path: Path \| None, written: bool}` |
| `AnalysisService` | `src/breezeai_cog/services/analysis.py:23` | Thin facade; holds the injected `Settings` |
| `AnalysisService.analyze_repo(repo, *, sink?, progress?, summary_out?)` | `src/breezeai_cog/services/analysis.py:37` | Without a `sink` creates a `FileSink` at `<out or .cog>/<repo>-project-analysis.ndjson.gz`, runs `pipeline.run`, returns `AnalysisResult` |
| `AnalysisService.iter_file_records(repo)` | `src/breezeai_cog/services/analysis.py:57` | Yields each `FileRecord` from `pipeline.iter_records` |

## services/upload.py

| Symbol | Anchor | Notes |
|---|---|---|
| `upload_ontology(settings, file_path, *, repository_name, on_attempt?)` | `src/breezeai_cog/services/upload.py:33` | Multipart `POST /code-ontology/generate`; retries network errors and 5xx, 4xx raises `UploadError` |
| `extract_ontology_id(payload)` | `src/breezeai_cog/services/upload.py:95` | Returns `_id` from a raw or `{"data": ...}` response, else `None` |
| `poll_ontology_status(settings, ontology_id, *, poll_interval?, overall_timeout?, on_waiting?, on_response?)` | `src/breezeai_cog/services/upload.py:124` | Polls `GET /code-ontology` until `fileGraphStatus` is `active` or `error`; returns that status |

## services/batch_upload.py

| Symbol | Anchor | Notes |
|---|---|---|
| `UploadTask` | `src/breezeai_cog/services/batch_upload.py:36` | Frozen dataclass `{repository_name: str, out_path: Path}` |
| `UploadState` | `src/breezeai_cog/services/batch_upload.py:45` | Resume state in `<workspace>/.cog/batch-upload-state.json`; `load(workspace)`, `is_done(name)`, `mark_done(name, *, total?)`, `clear()` |
| `ActiveView` | `src/breezeai_cog/services/batch_upload.py:108` | In-flight upload: `{name, attempt, elapsed}` |
| `TrackerSnapshot` | `src/breezeai_cog/services/batch_upload.py:115` | Point-in-time view: `total, completed, failed, active, total_elapsed` |
| `UploadTracker` | `src/breezeai_cog/services/batch_upload.py:129` | Thread-safe progress: `start`, `set_attempt`, `finish`, `snapshot`, properties `failed` and `errors` |
| `run_batch_uploads(tasks, settings, tracker, *, state?)` | `src/breezeai_cog/services/batch_upload.py:192` | Uploads and polls on up to `upload_parallelism` threads; returns the list of failed repo names |

## services/diff.py

| Symbol | Anchor | Notes |
|---|---|---|
| `run_diff_stream(settings, upload, temp_dir, filter_set, repo_name)` | `src/breezeai_cog/services/diff.py:61` | Parses `temp_dir`, streams records of `filter_set` files into `upload` (caller closes it); returns meta dict |
| `empty_meta(repo_name)` | `src/breezeai_cog/services/diff.py:83` | Zero-stats meta dict for deletion-only or no-change commits |

## services/inprocess.py

| Symbol | Anchor | Notes |
|---|---|---|
| `analyze_in_memory(settings, files, project_name?)` | `src/breezeai_cog/services/inprocess.py:20` | Writes `files` to a temp dir, runs `pipeline.run_inprocess`; returns `{projectMetaData, files}` |

## services/notify.py

| Symbol | Anchor | Notes |
|---|---|---|
| `post_notification(settings, path, payload)` | `src/breezeai_cog/services/notify.py:40` | JSON POST to `<baseurl><path>` with `api-key`; retries connect errors and 500/502/503 up to `NOTIFY_ATTEMPTS`, then raises `RuntimeError` |
| `NOTIFY_ATTEMPTS` | `src/breezeai_cog/services/notify.py:34` | Total attempts (3) |
| `NOTIFY_BACKOFF_SECONDS` | `src/breezeai_cog/services/notify.py:35` | Waits between attempts `(1.0, 3.0)` |
| `RETRYABLE_STATUSES` | `src/breezeai_cog/services/notify.py:36` | `{500, 502, 503}` |

## server/app.py

| Symbol | Anchor | Notes |
|---|---|---|
| `create_app(settings?, deps?)` | `src/breezeai_cog/server/app.py:47` | Factory: builds FastAPI, registers handlers for `ApiError`, validation errors and unhandled exceptions, includes the router |
| `app` | `src/breezeai_cog/server/app.py:80` | Module-level instance (`create_app()`); target for `uvicorn breezeai_cog.server.app:app` |

## server/errors.py

| Symbol | Anchor | Notes |
|---|---|---|
| `ApiError` | `src/breezeai_cog/server/errors.py:8` | `(message, status_code=500, failed_step=None)`; rendered as `{"error"}` plus `failedStep` when set |

## server/routes.py

| Symbol | Anchor | Notes |
|---|---|---|
| `health()` | `src/breezeai_cog/server/routes.py:72` | `GET /health` returns `{"status": "ok"}` |
| `analyze(request)` | `src/breezeai_cog/server/routes.py:77` | `POST /api/analyze` runs `analyze_in_memory` in a threadpool |
| `analyze_diff(request, background_tasks)` | `src/breezeai_cog/server/routes.py:98` | `POST /api/analyze-diff`: `acquire_diff`, `run_diff_stream`, storage upload, background notify |
| `analyze_sql(request, background_tasks, file, projectUuid, dataLakeId)` | `src/breezeai_cog/server/routes.py:212` | `POST /api/analyze-sql`: `parse_ddl`, storage upload, background notify; 202 |
| `analyze_nosql(request, background_tasks, file, projectUuid, dataLakeId)` | `src/breezeai_cog/server/routes.py:275` | `POST /api/analyze-nosql`: `build_nosql_records`, storage upload, background notify; 202 |
| `analyze_es(request, background_tasks, file, projectUuid, dataLakeId)` | `src/breezeai_cog/server/routes.py:330` | `POST /api/analyze-es`: `build_es_records`, storage upload, background notify; 202 |

## server/git_routes.py

| Symbol | Anchor | Notes |
|---|---|---|
| `router` | `src/breezeai_cog/server/git_routes.py:41` | `APIRouter(prefix="/api/git")`, mounted on the main router |
| `parse_pr_url_route(request)` | `src/breezeai_cog/server/git_routes.py:86` | `POST /api/git/parse-pr-url`; no provider call |
| `latest_commit(request)` | `src/breezeai_cog/server/git_routes.py:114` | `POST /api/git/latest-commit` via `branch_head` |
| `compare(request)` | `src/breezeai_cog/server/git_routes.py:126` | `POST /api/git/compare` |
| `tree(request)` | `src/breezeai_cog/server/git_routes.py:151` | `POST /api/git/tree` |
| `pull_request(request)` | `src/breezeai_cog/server/git_routes.py:163` | `POST /api/git/pull-request` |
| `pr_comment(request)` | `src/breezeai_cog/server/git_routes.py:192` | `POST /api/git/pr-comment`; only write route |
| `check_update(request)` | `src/breezeai_cog/server/git_routes.py:223` | `POST /api/git/check-update` |

## server/git.py

| Symbol | Anchor | Notes |
|---|---|---|
| `INVALID_REPO_URL_MESSAGE` | `src/breezeai_cog/server/git.py:34` | 400 message for an unrecognised repo URL |
| `parse_repo_url(repo_url, instances?)` | `src/breezeai_cog/server/git.py:40` | Dict view (`RepoRef.as_dict`) of the SCM parser, or `None` |
| `clone_repo_full(client, ref, incoming, branch, timeout?)` | `src/breezeai_cog/server/git.py:52` | Shallow single-branch clone, checkout of `incoming`, `.git` removed; returns temp dir |
| `resolve_git_diff(client, ref, current, incoming)` | `src/breezeai_cog/server/git.py:96` | Incremental REST acquisition; returns `(temp_dir, filter_set, deleted)` |
| `acquire_diff(settings, body)` | `src/breezeai_cog/server/git.py:153` | Chooses incremental or full clone; maps `SCMError` to `ApiError` |

## server/deps.py

| Symbol | Anchor | Notes |
|---|---|---|
| `ServerDeps` | `src/breezeai_cog/server/deps.py:16` | Dataclass: `settings`, `open_storage(key) -> InfraStream`, `notify(path, payload)`, `acquire_diff`, `open_scm(ref, settings, token)` |
| `default_deps(settings)` | `src/breezeai_cog/server/deps.py:27` | Wires `open_stream`, `post_notification`, `acquire_diff` and `SCMClientFactory.for_repo` |

## infra/

| Symbol | Anchor | Notes |
|---|---|---|
| `InfraStream` | `src/breezeai_cog/infra/interface.py:4` | ABC: `write_line(line)`, `close() -> key`; `close` raises if the object is incomplete |
| `ProviderType` | `src/breezeai_cog/infra/provider_type.py:4` | Enum with one member, `AWS = "aws"` |
| `ProviderConfig` | `src/breezeai_cog/infra/provider_config.py:5` | `is_active` property; `from_settings(settings)` reads `infra_provider` |
| `ProviderFactory` | `src/breezeai_cog/infra/factory.py:7` | `create_stream(key, settings)` returns `AWSStreamUpload` for AWS |
| `open_stream(key, settings)` | `src/breezeai_cog/infra/provider.py:7` | Streaming storage for the active provider |
| `AWSStreamUpload` | `src/breezeai_cog/infra/aws/s3.py:60` | Gzip-to-pipe multipart S3 upload into `aws_s3_bucket`; `write_line`, `close` |

## integrations/scm/

| Symbol | Anchor | Notes |
|---|---|---|
| `SUPPORTED_PROVIDERS` | `src/breezeai_cog/integrations/scm/base.py:21` | `{"github", "bitbucket", "gitlab", "azure_devops"}` |
| `DEFAULT_HOSTS` | `src/breezeai_cog/integrations/scm/base.py:27` | Public-cloud host per provider |
| `RepoRef` | `src/breezeai_cog/integrations/scm/base.py:36` | Frozen `{provider, owner, repo, project, host}`; `is_self_hosted`, `as_dict()` |
| `FileDiff` | `src/breezeai_cog/integrations/scm/base.py:75` | `{filename, status, additions, deletions, patch}`; `as_dict()` |
| `count_patch_lines(patch)` | `src/breezeai_cog/integrations/scm/base.py:105` | `(additions, deletions)` of a unified diff |
| `ChangeSet` | `src/breezeai_cog/integrations/scm/base.py:129` | `{changed, deleted, files}`; `is_empty` |
| `CommitInfo` | `src/breezeai_cog/integrations/scm/base.py:150` | `{sha, message, author, date}` |
| `PullRequestInfo` | `src/breezeai_cog/integrations/scm/base.py:164` | `{number, title, state, base_branch, head_branch, base_sha, head_sha, url}` |
| `PrComment` | `src/breezeai_cog/integrations/scm/base.py:184` | `{id, url}` of a posted comment |
| `PR_STATES` | `src/breezeai_cog/integrations/scm/base.py:193` | Provider PR state to `open`/`merged`/`closed` |
| `AbstractSCMClient` | `src/breezeai_cog/integrations/scm/base.py:205` | ABC: `tree`, `branch_head`, `pull_request`, `post_pr_comment`, `compare`, `file_content`, `clone_url`, `close`; context manager |
| `GitHubSCMClient` | `src/breezeai_cog/integrations/scm/github.py:29` | GitHub provider |
| `GitLabSCMClient` | `src/breezeai_cog/integrations/scm/gitlab.py:25` | GitLab provider |
| `BitbucketSCMClient` | `src/breezeai_cog/integrations/scm/bitbucket.py:36` | Bitbucket provider |
| `AzureDevOpsSCMClient` | `src/breezeai_cog/integrations/scm/azure_devops.py:60` | Azure DevOps provider |
| `SCMClientFactory` | `src/breezeai_cog/integrations/scm/factory.py:75` | `create(provider, token, settings, api_base_url?)` and `for_repo(ref, settings, request_token?)` |
| `resolve_scm_token(provider, request_token, settings)` | `src/breezeai_cog/integrations/scm/factory.py:43` | Request token, else `scm_token_<provider>`, else `None` |
| `resolve_api_base_url(ref, settings)` | `src/breezeai_cog/integrations/scm/factory.py:56` | API base for a self-hosted `ref`, else `None` |
| `SCMError` | `src/breezeai_cog/integrations/scm/errors.py:12` | Base error with `status_code` (default 502) |
| `SCMAPIError` | `src/breezeai_cog/integrations/scm/errors.py:32` | Provider REST failure; carries `http_status` |
| `SCMCredentialError` | `src/breezeai_cog/integrations/scm/errors.py:53` | Malformed credential; 400 |
| `UnsupportedSCMProviderError` | `src/breezeai_cog/integrations/scm/errors.py:60` | Unknown provider or host; 400 |
| `SCMHttpClient` | `src/breezeai_cog/integrations/scm/http.py:45` | `httpx.Client` wrapper: `get`, `get_json`, `get_text`, `post_json`, `paginate`, `close` |
| `warn_if_anonymous(provider, token)` | `src/breezeai_cog/integrations/scm/http.py:285` | Logs a warning when no token is set |
| `request_with_retry(send, *, provider, operation, max_retries, backoff_seconds, ...)` | `src/breezeai_cog/integrations/scm/retry.py:54` | Retries 429/502/503/504 and transport errors with jittered backoff |
| `retry_delay(backoff_seconds, attempt, rng?)` | `src/breezeai_cog/integrations/scm/retry.py:31` | Exponential delay capped at `MAX_DELAY_SECONDS` |
| `parse_repo_url(repo_url, instances?)` | `src/breezeai_cog/integrations/scm/repository.py:122` | Returns `RepoRef` or `None` |
| `PrLinks` | `src/breezeai_cog/integrations/scm/pr_url.py:36` | `{repo, diff, comment, decline}` URLs |
| `PrUrlInfo` | `src/breezeai_cog/integrations/scm/pr_url.py:44` | `{source, repo_url, pr_id, links}` |
| `parse_pr_url(pr_url, settings)` | `src/breezeai_cog/integrations/scm/pr_url.py:79` | Returns `PrUrlInfo` or `None` |
| `repo_api_url(ref, settings)` | `src/breezeai_cog/integrations/scm/pr_url.py:51` | Provider REST URL of the repository |
| `repo_web_url(ref)` | `src/breezeai_cog/integrations/scm/pr_url.py:69` | Canonical browser URL of the repository |

## analyzers/

| Symbol | Anchor | Notes |
|---|---|---|
| `parse_ddl(text, filepath?)` | `src/breezeai_cog/analyzers/sql.py:406` | Parses SQL DDL; returns `{tables, views, procedures, allIndexes, sequences, dialect, parseReport?}` |
| `build_nosql_records(uploads)` | `src/breezeai_cog/analyzers/nosql.py:297` | Returns `{records, collectionCount, fieldCount, indexCount, collections}` |
| `build_es_records(uploads)` | `src/breezeai_cog/analyzers/es.py:185` | Returns `{records, kind, mapping, setting, mappings, settings, indexCount, fieldCount, settingsMatched}` |
| `BuildError` (nosql) | `src/breezeai_cog/analyzers/nosql.py:47` | Has `status_code` (default 422) |
| `BuildError` (es) | `src/breezeai_cog/analyzers/es.py:14` | Has `status_code` (default 422) |
