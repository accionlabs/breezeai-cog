---
title: "breezeai-cog — Services and Business Logic"
date: 2026-10-30
---

Scope: Services layer — analysis, upload, batch, diff, analyzers. Where behavior actually lives.

## AnalysisService

Source: `src/breezeai_cog/services/__init__.py`

Thin facade over `core/pipeline.run`. Called by the CLI `repo-to-json-tree` command and `server/inprocess.analyze_in_memory`.

Key method: `analyze_repo(repo, progress?, summary_out?) -> AnalysisResult`
- Constructs a `FileSink` for the output path.
- Calls `core/pipeline.run(repo_root, settings, sink, ...)`.
- Returns `AnalysisResult(project_meta, out_path, written)`.

`AnalysisResult.written` is False when the parser produced nothing (no supported files) — the CLI skips the upload in this case.

## UploadService / upload

Source: `src/breezeai_cog/services/upload.py`

Uploads a single `.ndjson.gz` file to the backend via `httpx` POST to `<baseurl>/code-ontology/upload`:
- Streams the file in chunks; sets `Content-Type: application/octet-stream`.
- Sends `api-key` header from `settings.user_api_key`.
- `upload_timeout` caps the whole request (default 900 s).
- Retries on transient failure (network / timeout / HTTP 5xx) up to `upload_max_retries`; 4xx is fatal.
- Raises `UploadError` on final failure.

## Batch upload

Source: `src/breezeai_cog/services/batch_upload.py`

`run_batch_uploads(tasks, settings, tracker, state?)` — orchestrates concurrent uploads across `upload_parallelism` threads:
- `UploadTask(name, path)` — one per repo.
- `UploadState` — persists completed repos to `<workspace>/.cog/upload-state.json`; allows resume after interruption.
- `UploadTracker` — thread-safe counter for the live progress display.
- `TrackerSnapshot` — point-in-time view of tracker state for the CLI live bar.

## Diff service

Source: `src/breezeai_cog/services/diff.py`

`run_diff_stream(settings, sink, temp_dir, filter_set, repo_name) -> meta_dict`
- Filters the file list in `temp_dir` to `filter_set` (the changed files from the git compare).
- Runs `core/pipeline.run_inprocess(temp_dir, settings, sink)` (sequential, in-process — no pool startup cost).
- Returns a `meta_dict` suitable for the backend notify payload.
- `filter_set=None` means "no filter" (full parse); `len(filter_set)==0` means "no changed files" (deletion-only commit).
- `empty_meta(repo_name)` — returns a zero-stats meta dict for the deletion-only case.

## Inprocess analysis

Source: `src/breezeai_cog/services/inprocess.py`

`analyze_in_memory(settings, files, project_name?) -> dict`
- Accepts `files: list[{path, content}]` (strings), writes them to a temp dir, calls `run_inprocess`.
- Returns `{"files": [FileRecord.model_dump()...], "meta": ProjectMetaData.model_dump()}`.
- Used by `POST /api/analyze` (the in-memory route — no S3, no git clone).

## Analyzers

Source: `src/breezeai_cog/analyzers/`

Standalone analysis modules for non-code data sources (called directly by server routes):

| Module | Function | Input | Output |
|---|---|---|---|
| `analyzers/sql.py` | `parse_ddl(text, filename)` | SQL DDL text | `{tables, views, procedures, allIndexes, sequences, dialect, parseReport?}` — uses `sqlglot` |
| `analyzers/nosql.py` | `build_nosql_records(uploads)` | MongoDB JSON schema files | `{records, collections, collectionCount, fieldCount, indexCount}` |
| `analyzers/es.py` | `build_es_records(uploads)` | ES mapping/settings JSON files | `{records, mappings, settings, mapping, setting, kind, indexCount, fieldCount, settingsMatched}` |

`BuildError` (both nosql and es) carries `status_code` for the HTTP response.

## Notification service

Source: `src/breezeai_cog/services/notify.py`

`notify(baseurl, path, payload, user_api_key)` — fire-and-forget POST to `<baseurl><path>` with the given JSON payload and `api-key` header. Used as a `BackgroundTask` in the analyze-diff, analyze-sql, analyze-nosql, and analyze-es routes. Errors are logged but not re-raised (non-fatal to the route response).
