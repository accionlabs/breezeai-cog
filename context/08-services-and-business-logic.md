---
title: "breezeai-cog — Services and Business Logic"
date: 2026-10-30
---

Scope: Services layer — analysis, upload, batch, diff, analyzers. Where behavior actually lives.

## AnalysisService

Source: `src/breezeai_cog/services/analysis.py`

Thin facade over `core/pipeline.run`, called by the CLI `repo-to-json-tree` command.

Key method: `analyze_repo(repo, progress?, summary_out?) -> AnalysisResult`
- Constructs a `FileSink` for the output path.
- Calls `core/pipeline.run(repo_root, settings, sink, ...)`.
- Returns `AnalysisResult(project_meta, out_path, written)`.

`AnalysisResult.written` is False when the parser produced nothing (no supported files) — the CLI skips the upload in this case.

## Ontology upload

Source: `src/breezeai_cog/services/upload.py`

`upload_ontology` sends a multipart file (`application/gzip`) to
`POST <baseurl>/code-ontology/generate`:
- Includes the `projectUuid` and repository name with the file.
- Sends an `api-key` header from settings.
- `upload_timeout` caps the whole request (default 900 s).
- Retries on transient failure (network / timeout / HTTP 5xx) up to `upload_max_retries`; 4xx is fatal.
- Raises `UploadError` on final failure.

## Batch upload

Source: `src/breezeai_cog/services/batch_upload.py`

`run_batch_uploads(tasks, settings, tracker, state?)` — orchestrates concurrent uploads across `upload_parallelism` threads:
- `UploadTask` carries `repository_name` and `out_path` for one repository.
- `UploadState` — persists completed repos to `<workspace>/.cog/batch-upload-state.json`; allows resume after interruption.
- `UploadTracker` — thread-safe counter for the live progress display.
- `TrackerSnapshot` — point-in-time view of tracker state for the CLI live bar.

## Diff service

Source: `src/breezeai_cog/services/diff.py`

`run_diff_stream(settings, sink, temp_dir, filter_set, repo_name) -> meta_dict`
- Parses the checkout in `temp_dir`; it does not restrict scanning to `filter_set`.
- `_InfraStreamSink.write` filters records as they are written, retaining changed files.
- Returns a `meta_dict` suitable for the backend notify payload.
- `filter_set=None` means "no filter" (full parse); `len(filter_set)==0` means "no changed files" (deletion-only commit).
- `empty_meta(repo_name)` — returns a zero-stats meta dict for the deletion-only case.

## Inprocess analysis

Source: `src/breezeai_cog/services/inprocess.py`

`analyze_in_memory(settings, files, project_name?) -> dict`
- Accepts `files: list[{path, content}]` (strings), writes them to a temp dir, calls `run_inprocess`.
- Calls `core/pipeline.run_inprocess` directly and returns `{"projectMetaData": ..., "files": [...]}`.
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

`post_notification(settings, path, payload)` — POSTs JSON to the configured backend path with the service API key. Used as a `BackgroundTask` by the analyze-diff, analyze-sql, analyze-nosql, and analyze-es routes. It retries up to three times and raises `RuntimeError` after the final failure; the HTTP response has already been sent because this runs in the background.
