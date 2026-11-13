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
| **additive detector** | A cross-language statement detector in `parsers/additive.py` that runs post-parse |
| **base parser** | The `priority=0` parser for a language; fallback when no framework parser claims the file |
| **framework parser** | A `priority>0` parser that claims files matching its framework signature |
| **shared file** | A file whose extension several base languages register (`.h`: C and C++); `core/ownership.py` decides its language from the repo |
| **ownership step** | Pre-index pass (`resolve_owners`) that labels each shared file by content proof, references, neighbours/repo, then default |
| **build_index** | Optional repo-level pre-pass a parser can run to build cross-file resolution data |
| **resolution_index** | Per-process result of `build_index`; available in `ParseContext.resolution_index` |
| **FileSink** | Writes the NDJSON.gz output file; line-1 guarantee via temp-file swap |
| **S3Sink** | Streams NDJSON.gz to AWS S3 via multipart upload |
| **template extension** | A markup/view file extension (e.g. `.html`, `.cshtml`, `.vue`) gated by `--capture-templates` |
| **batch mode** | CLI `--batch` flag: analyze every immediate subdirectory of a workspace folder |
| **UploadState** | Persistent resume state for batch uploads; keyed by repo name in `<workspace>/.cog/upload-state.json` |
| **acquire_diff** | `ServerDeps` method that clones/fetches the repo and returns changed files for `analyze-diff` |
| **SCM** | Source Control Management; GitHub, GitLab, Bitbucket, Azure DevOps |
| **RepoRef** | Parsed representation of a repo URL (source, owner, repo, host, base_url) |
| **`.cog/`** | Cog's artifact directory inside each repo (export, skip report, logs); should be git-ignored |

## Known foot-guns

- **tree-sitter pin**: `<0.26` is hard. `Parser.timeout_micros` was removed in 0.26. Upgrading requires migrating to chunked-callback source form.
- **Node 22 requirement**: the `breeze:onboard-repository` skill shells out to cog via Node; Node 24 fails silently on tree-sitter native bindings. No error is thrown — output is simply empty.
- **Groovy is skeleton-only**: the grammar is structurally sound but the parser captures outlines only; detailed statement analysis is not implemented. A full Groovy parse run produces Function/Class records but minimal statements.
- **Capture templates default off**: markup files (`.html`, `.cshtml`, `.razor`, `.vue`, etc.) are skipped by default to avoid flooding the graph. Razor Pages / Blazor `@page` routes and Vue SFC script blocks require `--capture-templates`.
- **`max_concat_depth` recursion**: very high values can re-trigger the RecursionError the cap is meant to prevent. Keep at 100 (default) unless a specific case requires higher.
- **`.repoinclude` does not re-include templates**: template files excluded by the template gate cannot be re-included via `.repoinclude`; only `--capture-templates` re-enables them.
- **`upload_max_retries` counts retries, not total attempts**: total = `upload_max_retries + 1`. Same asymmetry as botocore's `max_attempts`.
- **Batch upload resume**: if a batch is interrupted, re-run the same command. `UploadState` skips already-uploaded repos. `--force` discards saved state.
- **S3 stream not replayable**: `infra/aws.py` multipart upload cannot be restarted mid-stream; the SDK retries at the part level via botocore config.

## Open questions / UNVERIFIED

- **Groovy grammar PR**: the Accionlabs fork's `groovy-grammar-upstream` branch adds upstream fixes. Once the upstream PR merges, the pin should move back to a released `dekobon-tree-sitter-groovy` version — track in `pyproject.toml` comment.
- **CI gap**: there is no automated test run on PR; test discipline is developer-only. A CI test job (`ci.yml.disabled`) exists but is disabled — reason not documented in source.
- **`toon-format==0.9.0b1`**: pinned to a beta. Usage and upgrade path not verified in this review. UNVERIFIED.
- **`sqlglot`** dialect support scope for `analyze-sql`: not all SQL dialects may be supported; `parseReport` in the response surfaces partial parse results. Dialect auto-detection accuracy UNVERIFIED.

## Symbol reference coverage

The symbol shards in `context/14-symbols/` list every parser class, detector entry point, service,
server route handler, SCM client and infra class, plus the main helper functions. They do not list
every function: about a third of the unique function and class names in `src/` appear in them (734
of 2,135 when last measured), and underscore-prefixed internal helpers are mostly omitted. Helper
descriptions in the parser shards were written from signatures and docstrings, not from reading each
function body. If a symbol is not in a shard, search source by name; do not conclude it does not
exist.
