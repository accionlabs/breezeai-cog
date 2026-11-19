---
title: "breezeai-cog — Diagnosis Playbook (Router)"
date: 2026-10-30
---

Scope: Router and global symptom-to-suspect table. Read this table, then **only the one shard** that covers the failure.

| Shard | Subsystems covered |
|---|---|
| `15-diagnosis/core-and-schemas.md` | Core pipeline, worker executor, scanner, ignore rules, Pydantic schemas, schema versioning |
| `15-diagnosis/services-server-integrations.md` | Analysis service, backend upload, git server / clone, notification callbacks, S3 storage |

## Global symptom to suspect table

| Failure and symptom | Suspect location | Shard to read |
|---|---|---|
| Expected file missing from scan results: A source file exists in the repo but is never processed by any parser and does not appear in NDJSON output. | `scan` and its inner `keep` in `src/breezeai_cog/core/scanner.py:67`; skip reasons are tallied by `SkipReport` in `src/breezeai_cog/core/skips.py:33` | `core-and-schemas.md` |
| Git clone or checkout failure: `/api/analyze-diff` returns `{"error": "Git acquisition failed: ...", "failedStep": "git_acquire"}`. Status 502 for provider or clone failures (`RuntimeError` … | `src/breezeai_cog/server/git.py:52` (`clone_repo_full`), `src/breezeai_cog/server/git.py:153` (`acquire_diff`) and `src/breezeai_cog/server/routes.py:133` | `services-server-integrations.md` |
| Backend upload failure (CLI): `UploadError: upload failed for <file>: <error>`, or a config error such as `baseurl is not configured`. | `src/breezeai_cog/services/upload.py:33` (`upload_ontology`, config checks at lines 54-62, final raise at line 92) | `services-server-integrations.md` |
| Storage open / upload failure (server): `{"error": "Storage open failed: ...", "failedStep": "upload"}` or `{"error": "Upload failed: ...", "failedStep": "upload"}` with status 500. | `src/breezeai_cog/server/routes.py:156` and `src/breezeai_cog/infra/aws/s3.py:60` | `services-server-integrations.md` |
| Backend notification failure: Warning `"... (attempt X/3, retrying in Ys)"`, ending in `RuntimeError: ... (after 3 attempts)` in the server log. The HTTP response was already sent. | `src/breezeai_cog/services/notify.py:40` | `services-server-integrations.md` |
| Malformed route request payload: HTTP 400 `{"error": ...}` (validation errors also map to 400), or 422 when nothing could be extracted. | `src/breezeai_cog/server/routes.py:84-93` and `src/breezeai_cog/server/git_routes.py:50` | `services-server-integrations.md` |
| Parser, detector, or capture gap (including a missing route or statement): do not infer intended behavior from this context set; verify the source and use the repository's parser review process. | `skills/gap-review/SKILL.md` and `docs/parser-review-guide.md` | Use the parser review guide |
