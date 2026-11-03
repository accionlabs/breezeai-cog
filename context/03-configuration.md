---
title: "breezeai-cog — Configuration"
date: 2026-10-30
---

Scope: Every env var and Settings field, grouped by subsystem. Source: `src/breezeai_cog/config.py` and `.env.example`.

Precedence (highest first): CLI flags / request params → env vars → `.env` → defaults.
Env prefix: `BREEZEAI_COG_` (well-known integration vars keep their conventional names as aliases).
Settings are injected into services; no module-level singleton.

## Analysis

| Field | Env var | Default | Notes |
|---|---|---|---|
| `repo` | `BREEZEAI_COG_REPO` | `None` | `--repo`; CLI only |
| `out` | `BREEZEAI_COG_OUT` | `None` | `--out`; output directory for .ndjson.gz |
| `languages` | `BREEZEAI_COG_LANGUAGE` / `BREEZEAI_COG_LANGUAGES` | `None` | Comma-separated allow-list; `None` = auto-detect all |
| `capture_statements` | `BREEZEAI_COG_CAPTURE_STATEMENTS` | `True` | Emit in-body statements (routes, API calls, DB); `--no-capture-statements` turns off |
| `capture_templates` | `BREEZEAI_COG_CAPTURE_TEMPLATES` | `False` | Include markup/view files (.html, .cshtml, .razor, .vue, etc.) |
| `jobs` | `BREEZEAI_COG_JOBS` | `None` (→ CPU count) | Worker processes for parallel parse |
| `statement_text_limit` | `BREEZEAI_COG_STATEMENT_TEXT_LIMIT` | `8000` | Max statement `text` chars before splitting into `#partNofN` parts |
| `max_statement_parts` | `BREEZEAI_COG_MAX_STATEMENT_PARTS` | `0` | Cap on parts per oversized statement; `0` = unbounded |
| `max_file_size` | `BREEZEAI_COG_MAX_FILE_SIZE` | `2000000` | Bytes; scanner skips larger files |
| `parse_timeout` | `BREEZEAI_COG_PARSE_TIMEOUT` | `10.0` | Per-file tree-sitter timeout in seconds; `0` disables |
| `max_concat_depth` | `BREEZEAI_COG_MAX_CONCAT_DEPTH` | `100` | Max `+` nesting folded into an endpoint string; guards recursion |

## Logging

| Field | Env var | Default | Notes |
|---|---|---|---|
| `log_level` | `BREEZEAI_COG_LOG_LEVEL` | `INFO` | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `log_format` | `BREEZEAI_COG_LOG_FORMAT` | `plaintext` | `plaintext` or `json` |
| `log_to_file` | `BREEZEAI_COG_LOG_TO_FILE` | `True` | Write to `log_location` |
| `log_location` | `BREEZEAI_COG_LOG_LOCATION` | `None` | CLI auto-sets `<repo>/.cog/logs`; server uses `./logs` |

## Server

| Field | Env var | Default | Notes |
|---|---|---|---|
| `port` | `BREEZEAI_COG_PORT` | `3000` | Overridden by `$PORT` env or `--port` at runtime |

## Git / SCM providers (server)

| Field | Env var | Default | Notes |
|---|---|---|---|
| `git_clone_timeout` | `BREEZEAI_COG_GIT_CLONE_TIMEOUT` | `1800.0` | Seconds cap on git clone/fetch/checkout |
| `github_api_base_url` | `BREEZEAI_COG_GITHUB_API_BASE_URL` | `https://api.github.com` | Override for GitHub Enterprise |
| `gitlab_api_base_url` | `BREEZEAI_COG_GITLAB_API_BASE_URL` | `https://gitlab.com/api/v4` | Override for self-hosted GitLab |
| `bitbucket_api_base_url` | `BREEZEAI_COG_BITBUCKET_API_BASE_URL` | `https://api.bitbucket.org/2.0` | |
| `azure_devops_api_base_url` | `BREEZEAI_COG_AZURE_DEVOPS_API_BASE_URL` | `https://dev.azure.com` | |
| `scm_api_timeout` | `BREEZEAI_COG_SCM_API_TIMEOUT` | `60.0` | Per-request cap on provider REST calls |
| `scm_api_retry_max` | `BREEZEAI_COG_SCM_API_RETRY_MAX` | `3` | Retries on 429/502/503/504; `0` = one try |
| `scm_api_retry_backoff_seconds` | `BREEZEAI_COG_SCM_API_RETRY_BACKOFF_SECONDS` | `2.0` | Base backoff; doubles per attempt, jittered, capped at 60 s |
| `scm_max_pages` | `BREEZEAI_COG_SCM_MAX_PAGES` | `100` | Pagination ceiling per listing; truncates with warning |
| `scm_token_github` | `BREEZEAI_COG_SCM_TOKEN_GITHUB` | `None` | **SECRET** — fallback token when request carries no `gitToken` |
| `scm_token_gitlab` | `BREEZEAI_COG_SCM_TOKEN_GITLAB` | `None` | **SECRET** |
| `scm_token_bitbucket` | `BREEZEAI_COG_SCM_TOKEN_BITBUCKET` | `None` | **SECRET** — format `username:api_key` |
| `scm_token_azure_devops` | `BREEZEAI_COG_SCM_TOKEN_AZURE_DEVOPS` | `None` | **SECRET** |
| `scm_instances` | `BREEZEAI_COG_SCM_INSTANCES` | `{}` | JSON: `{"git.acme.com": "gitlab"}` — host→provider for self-hosted |
| `scm_instance_base_url_mapping` | `BREEZEAI_COG_SCM_INSTANCE_BASE_URL_MAPPING` | `{}` | JSON: `{"git.acme.com": "https://git.acme.com/api/v4"}` — host→REST base |

## Backend upload / notification

| Field | Env var | Default | Notes |
|---|---|---|---|
| `upload` | `BREEZEAI_COG_UPLOAD` | `False` | Enable upload toggle; requires `baseurl`, `uuid`, `user_api_key` |
| `baseurl` | `BREEZEAI_COG_BASEURL` / `BREEZE_API_URL` | `None` | Backend base URL |
| `uuid` | `BREEZEAI_COG_UUID` | `None` | Project UUID |
| `user_api_key` | `BREEZEAI_COG_USER_API_KEY` / `API_KEY` | `None` | **SECRET** — sent as `api-key` header |
| `upload_timeout` | `BREEZEAI_COG_UPLOAD_TIMEOUT` | `900.0` | Per-repo upload timeout in seconds (15 min) |
| `upload_parallelism` | `BREEZEAI_COG_UPLOAD_PARALLELISM` | `1` | Concurrent uploads in `--batch` mode |
| `upload_max_retries` | `BREEZEAI_COG_UPLOAD_MAX_RETRIES` | `1` | Retries on transient failure; 4xx is fatal |

## Infra / object storage

| Field | Env var | Default | Notes |
|---|---|---|---|
| `infra_provider` | `BREEZEAI_COG_INFRA_PROVIDER` | `aws` | Only `aws` supported |
| `storage_retry_attempts` | `BREEZEAI_COG_STORAGE_RETRY_ATTEMPTS` | `3` | Botocore `max_attempts` (counts retries, so N → N+1 total tries) |
| `storage_connect_timeout` | `BREEZEAI_COG_STORAGE_CONNECT_TIMEOUT` | `10` | Seconds |
| `storage_read_timeout` | `BREEZEAI_COG_STORAGE_READ_TIMEOUT` | `60` | Seconds |

## AWS / S3

| Field | Env var | Default | Notes |
|---|---|---|---|
| `aws_access_key` | `AWS_ACCESS_KEY` / `AWS_ACCESS_KEYID` | `None` | **SECRET** — absent = boto3 default chain (IAM/IRSA) |
| `aws_secret_key` | `AWS_SECRET_KEY` | `None` | **SECRET** |
| `aws_region` | `AWS_REGION` | `us-west-2` | |
| `aws_s3_bucket` | `AWS_S3_BUCKET` | `None` | Required for server S3 upload routes |
