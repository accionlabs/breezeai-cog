---
title: "breezeai-cog — Glossary and Open Questions"
date: 2026-10-30
---

Scope: Domain glossary, naming conventions, known foot-guns, open questions.

## Glossary

| Term | Definition |
|---|---|
| **capture contract** | The NDJSON format that cog emits; defined by `schemas/capture.py`; versioned by `SCHEMA_VERSION` |
| **FileRecord** | One NDJSON line representing a source file's parsed content |
| **ProjectMetaData** | First NDJSON line; repository-level summary |
| **SCHEMA_VERSION** | Semver string in `schemas/version.py`; all parsers must match it at registration |
| **statement** | A flat `Statement` record capturing a route, DB call, API call, event, or other in-body expression |
| **semantic type** | Second classification axis on a `Statement` (e.g. `route`, `db_method_call`, `api_call`) |
| **batch mode** | CLI `--batch` flag: analyze every immediate subdirectory of a workspace folder |
| **UploadState** | Persistent resume state for batch uploads; stored in `<workspace>/.cog/batch-upload-state.json` |
| **acquire_diff** | `ServerDeps` method that clones/fetches the repo and returns changed files for `analyze-diff` |
| **SCM** | Source Control Management; GitHub, GitLab, Bitbucket, Azure DevOps |
| **RepoRef** | Parsed repository reference (`provider`, `owner`, `repo`, `project`, `host`) |
| **`.cog/`** | Cog's artifact directory inside each repo (export, skip report, logs); should be git-ignored |

## Known foot-guns

- **`upload_max_retries` counts retries, not total attempts**: total = `upload_max_retries + 1`. Same asymmetry as botocore's `max_attempts`.
- **Batch upload resume**: if a batch is interrupted, re-run the same command. `UploadState` skips already-uploaded repos. `--force` discards saved state.
- **S3 stream not replayable**: `infra/aws/s3.py` uploads through a stream; consult the implementation before retrying an interrupted upload.

## Open questions / UNVERIFIED

- **CI gap**: there is no automated test run on PR; test discipline is developer-only. A CI test job (`ci.yml.disabled`) exists but is disabled — reason not documented in source.
- **`sqlglot`** dialect support scope for `analyze-sql`: not all SQL dialects may be supported; `parseReport` in the response surfaces partial parse results. Dialect auto-detection accuracy UNVERIFIED.

## Context limitations

This context set is a snapshot for orientation, not a complete or authoritative inventory.
Use current source and the maintained documentation for behavior and parser details.
