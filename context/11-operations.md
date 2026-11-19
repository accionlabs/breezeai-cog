---
title: "breezeai-cog — Operations"
date: 2026-10-30
---

Scope: Dockerfile, CI, k8s deploy, runtime topology, runbook.

## Docker build

Source: `Dockerfile`

Multi-stage build:
1. **Builder** — `python:3.13-slim`; installs `uv`, `git`, `ca-certificates`, `build-essential` (needed for Groovy grammar C binding compilation); runs `uv sync --extra server --no-dev`.
2. **Runtime** — copies only the venv from builder; no build tools, no source tree.

```bash
docker build -t breezeai-cog .
docker run -p 3000:3000 \
  -e BREEZEAI_COG_PORT=3000 \
  -e AWS_S3_BUCKET=my-bucket \
  -e AWS_REGION=us-west-2 \
  breezeai-cog
```

## CI

Source: `.github/workflows/main.yml`

Name: **PR Title Validation**
Trigger: `pull_request` (opened / edited / reopened / synchronize)
Job: validates PR title contains `BREEZEAI-<N>` (Jira ticket key). Fails the check if absent.

**No build, test, or lint CI runs automatically.** `ci.yml` exists but is `.disabled`. All automated testing is manual developer responsibility.

## Kubernetes deploy

Source: `breezeai-cog-deploy.yaml`

- Container port: 3000.
- Env vars injected from k8s `ConfigMap` / `Secret`.

## Runtime topology

```
breezeai-backend  →  POST /api/analyze-diff   →  breezeai-cog (server)
                   →  POST /api/git/*
                   →  POST /api/analyze-sql/nosql/es

breezeai-cog (server)  →  AWS S3 (upload NDJSON.gz)
                        →  breezeai-backend /code-ontology/stream-ingest (notify)

```

## Runbook

### Capture failures

For missing or incorrect parser/detector output, follow `skills/gap-review/SKILL.md` and
`docs/parser-review-guide.md`, and verify behavior in source. For a runtime parse failure, use
the `parse.file.failed` log entry to identify the file and error. The `<repo>-skipped-report.json`
sidecar covers scanner skips such as oversized files; adjust `BREEZEAI_COG_MAX_FILE_SIZE` only
when the skip reason warrants it.

### Upload failures

- 4xx: check `baseurl`, `uuid`, `user_api_key` settings. Backend may have rejected the ontology.
- 5xx / timeout: rerun the CLI analysis with `--upload`; the command analyzes and uploads in that run. Batch mode resumes completed repositories using `UploadState`.

### S3 stream ingest not triggered

Check that `BREEZEAI_COG_BASEURL` (or `BREEZE_API_URL`) is set and reachable. Notification runs as a `BackgroundTask`, after the response is sent; a failure is logged but cannot be returned to the caller.

### Known failure modes

- A parse timeout is isolated to the file and logged as `parse.file.failed`; it is not listed in the scanner skip report.
- The `<repo>-skipped-report.json` sidecar lists scanner skips (unsupported, ignored, template, oversized); parse failures are logged separately.
- Index serialization catches `OSError` while managing the worker pickle file. Index-build failures are logged as `build_index.failed` and that index is unavailable for the run.
- Temp dir cleanup: `shutil.rmtree(temp_dir, ignore_errors=True)` in finally; a failed cleanup leaves orphaned temp dirs — safe to manually remove.
