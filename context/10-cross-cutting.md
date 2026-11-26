---
title: "breezeai-cog — Cross-Cutting"
date: 2026-10-30
---

Scope: Logging, error handling, concurrency, file handling, auth absence.

## Logging

Source: `src/breezeai_cog/logging.py`

- Framework: `structlog` with `rich` for pretty terminal output.
- Two loggers: main (`breezeai_cog`) and a detail-only logger (`DETAIL_LOGGER`) that goes to file only (per-file skip rows, never printed to console).
- Setup: `setup_logging(settings)` — configures structlog + file handler. Called early in CLI (`_setup_logging_or_exit`) and in server boot.
- Worker logging: workers in the process pool cannot write directly (different processes). `setup_worker_logging(log_queue, ...)` sets up a `QueueHandler`; the main process runs a `QueueListener` that forwards records to the configured handlers.
- Console quieting: `quiet_console()` silences `analysis.complete` / `files.skipped` on the console only (not the file) when a Rich table is being rendered. `route_logs_through_console(console)` routes logs through a shared Rich `Console` so warnings appear above the pinned progress bar.
- Log format: `plaintext` (default, human-readable) or `json` (for log aggregators).
- Log location: CLI auto-sets `<repo>/.cog/logs`; server falls back to `./logs`. `BREEZEAI_COG_LOG_LOCATION` always wins.

## Error handling

| Layer | Mechanism |
|---|---|
| Per-file parse | `executor._parse_entry` catches all exceptions; logs `parse.file.failed`; returns `None` — file counted as "failed", run continues |
| Server routes | `ApiError(message, status_code, failed_step?)` → `{"error": str, "failedStep"?: str}` JSON |
| Validation errors | `RequestValidationError` → 400 (overrides FastAPI's default 422) |
| Upload | `UploadError` → retry logic in `services/upload.py`; final failure raises to caller |
| SCM errors | `SCMError` → 400 (bad URL/credential) or 502 (provider failure) in git routes |
| Build-index failure | Caught per-language; logs `build_index.failed`; falls back to "no index" (honest-null) |
| Credential scrubbing | `server/git.py:_scrub()` removes tokens from error messages before logging/returning |

## Concurrency

| Component | Mechanism |
|---|---|
| CLI parse (`run`) | `ProcessPoolExecutor` with spawn context; `~N/jobs/4` files per batch |
| Server parse (`run_inprocess`) | Sequential, in-process; no pool startup |
| Server routes | FastAPI async; CPU-bound work runs in `run_in_threadpool` |
| CLI upload | Runs on the upload-phase thread; batch uploads use `run_batch_uploads` and its configured parallelism |
| Log funneling | `Manager().Queue` + `QueueListener` across pool workers |
| Index transfer to workers | Pickled to a private temp file (avoids pipe-buffer bottleneck for large indexes) |

## File handling

- **Scan**: recursive `os.scandir` traversal with per-language ignore layers; skips files > `max_file_size` (2 MB default).
- **Read**: each worker reads `source = open(abs_path, "rb").read()` in `_parse_entry`. File bytes are not sent across the process boundary — only the relative `path` string is pickled in the `ScanEntry`.
- **CLI output**: `emit/sinks.py` `FileSink` writes `projectMetaData` to a temp file first; on `finalize`, swaps the temp output into the `.ndjson.gz` file.
- **S3 streaming**: `infra/aws/s3.py` accepts text lines, gzips them into a pipe, and a background thread uploads the pipe with `upload_fileobj`. The stream is not replayable; the SDK retries at the part level.
- **Temp dirs**: diff service (`git clone`) and executor (indexes pickle) write to `tempfile.mkdtemp()` with `0700` permissions. Both clean up in `finally` blocks.

## Auth

No inbound authentication on any route. The service sits behind Kong ingress and is called server-to-server from `breezeai-backend`. Outbound auth:
- Backend upload / notify: `api-key: <user_api_key>` header.
- SCM providers: `gitToken` from request body (per-request) or `scm_token_<provider>` fallback (global).

## Cross-platform

`ProcessPoolExecutor` uses `mp.get_context("spawn")` explicitly — matches macOS/Windows default and avoids `fork()` deadlock risk in multithreaded Python 3.12+. Works on all target platforms without event-loop policy changes.
