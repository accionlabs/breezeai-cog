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

- Container port: 3004 (differs from default local 3000).
- Env vars injected from k8s `ConfigMap` / `Secret`.

## Runtime topology

```
breezeai-backend  →  POST /api/analyze-diff   →  breezeai-cog (server)
                   →  POST /api/git/*
                   →  POST /api/analyze-sql/nosql/es

breezeai-cog (server)  →  AWS S3 (upload NDJSON.gz)
                        →  breezeai-backend /code-ontology/stream-ingest (notify)

breeze:onboard-repository skill  →  breezeai-cog CLI (subprocess; requires Node 22)
```

## Runbook

### Parse failures

If files fail to parse (`parse.file.failed` in logs):
- Check if grammar covers the language: `breezeai-cog capabilities | jq '.languages'`
- Check for tree-sitter timeout: `BREEZEAI_COG_PARSE_TIMEOUT` (default 10 s); set to 0 to disable for debugging.
- Oversized files: check `skipped.json` sidecar for `reason: oversized`; raise `BREEZEAI_COG_MAX_FILE_SIZE` if needed.

### Upload failures

- 4xx: check `baseurl`, `uuid`, `user_api_key` settings. Backend may have rejected the ontology.
- 5xx / timeout: the run already produced the `.ndjson.gz`; re-run with `--upload` only (batch resume via `UploadState`).

### Groovy parse issues

The Groovy grammar pin is `dekobon-tree-sitter-groovy @ git+https://github.com/accionlabs/tree-sitter-groovy@groovy-grammar-upstream`. Real Groovy class-collapse bugs are tracked in `.todo/groovy-grammar-evaluation.md`. Workaround: `--language java` to skip Groovy until the grammar matures.

### S3 stream ingest not triggered

Check that `BREEZEAI_COG_BASEURL` (or `BREEZE_API_URL`) is set and reachable. The notify is a `BackgroundTask` — a 4xx from the backend is logged but not surfaced to the caller. Check `breezeai-cog` logs for `notify.failed`.

### Known failure modes

- `ValueError` from tree-sitter timeout: isolated per-file; run continues. File appears in `skipped-report.json`.
- `pickle.PicklingError` on indexes: some custom parser index objects may not be picklable; index falls back to None.
- Temp dir cleanup: `shutil.rmtree(temp_dir, ignore_errors=True)` in finally; a failed cleanup leaves orphaned temp dirs — safe to manually remove.
