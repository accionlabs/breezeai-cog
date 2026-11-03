---
title: "breezeai-cog — Diagnosis Playbook (Router)"
date: 2026-10-30
---

Scope: Router and global symptom-to-suspect table. Read this table, then **only the one shard** that covers the failure.

| Shard | Subsystems covered |
|---|---|
| `15-diagnosis/core-and-schemas.md` | Core pipeline, worker executor, scanner, ignore rules, Pydantic schemas, schema versioning |
| `15-diagnosis/services-server-integrations.md` | Analysis service, backend upload, git server / clone, notification callbacks, S3 storage |
| `15-diagnosis/parsers.md` | Tree-sitter bindings, parser state, Node 22 requirement, Groovy grammar, framework route extraction |

## Global symptom to suspect table

| Failure and symptom | Suspect location | Shard to read |
|---|---|---|
| Schema version mismatch on parser registration: `RegistryError: parser '<name>' schema_version 'X' != '2.4'` raised during boot or parser discovery. | `src/breezeai_cog/core/registry.py:24` in `register(parser)` (raises `RegistryError`) | `core-and-schemas.md` |
| Per-file parse crash or tree-sitter timeout: Log warning `"parse.file.failed"` with `path=<path>`, `parser=<name>`, and `error=<msg>`. File is missing from output NDJSON. | `src/breezeai_cog/core/executor.py:84` in `_parse_entry(path, repo_root, options)`; the `except Exception` guard logs the warning and returns `None`. The per-language index is read from … | `core-and-schemas.md` |
| Cross-file resolution index failure: Log warning `"build_index.failed"` with `parser=<name>` and `error=<msg>`. Cross-file calls remain unresolved. | `src/breezeai_cog/core/pipeline.py:158` in `_build_indexes(repo_root, entries, jobs, *, debug_on, parse_timeout_micros)`; the `except Exception` around `base.build_index` logs the warning | `core-and-schemas.md` |
| Shared `.h` header captured as the wrong language (C vs C++): classes or functions missing, or `ownership.decided` shows an unexpected `rule`. | `resolve_owners` in `src/breezeai_cog/core/ownership.py:129`; `cpp_only_syntax` in `src/breezeai_cog/parsers/cpp/ownership.py:74` | `parsers.md` |
| Expected file missing from scan results: A source file exists in the repo but is never processed by any parser and does not appear in NDJSON output. | `scan` and its inner `keep` in `src/breezeai_cog/core/scanner.py:67`; skip reasons are tallied by `SkipReport` in `src/breezeai_cog/core/skips.py:33` | `core-and-schemas.md` |
| Git clone or checkout failure: `/api/analyze-diff` returns `{"error": "Git acquisition failed: ...", "failedStep": "git_acquire"}`. Status 502 for provider or clone failures (`RuntimeError` … | `src/breezeai_cog/server/git.py:52` (`clone_repo_full`), `src/breezeai_cog/server/git.py:153` (`acquire_diff`) and `src/breezeai_cog/server/routes.py:133` | `services-server-integrations.md` |
| Backend upload failure (CLI): `UploadError: upload failed for <file>: <error>`, or a config error such as `baseurl is not configured`. | `src/breezeai_cog/services/upload.py:33` (`upload_ontology`, config checks at lines 54-62, final raise at line 92) | `services-server-integrations.md` |
| Storage open / upload failure (server): `{"error": "Storage open failed: ...", "failedStep": "upload"}` or `{"error": "Upload failed: ...", "failedStep": "upload"}` with status 500. | `src/breezeai_cog/server/routes.py:156` and `src/breezeai_cog/infra/aws/s3.py:60` | `services-server-integrations.md` |
| Backend notification failure: Warning `"... (attempt X/3, retrying in Ys)"`, ending in `RuntimeError: ... (after 3 attempts)` in the server log. The HTTP response was already sent. | `src/breezeai_cog/services/notify.py:40` | `services-server-integrations.md` |
| Malformed route request payload: HTTP 400 `{"error": ...}` (validation errors also map to 400), or 422 when nothing could be extracted. | `src/breezeai_cog/server/routes.py:84-93` and `src/breezeai_cog/server/git_routes.py:50` | `services-server-integrations.md` |
| Tree-sitter version incompatibility: `AttributeError` on `Parser.timeout_micros` (under tree-sitter 0.26+) or `ValueError: Parsing failed` (a per-file timeout, raised by the binding and caught by the … | `get_parser` at `src/breezeai_cog/parsers/treesitter.py:49` and the `timeout_micros` assignment in `parse_source` at `src/breezeai_cog/parsers/treesitter.py:71` | `parsers.md` |
| Tree-sitter parser state leakage across files: Corrupted line spans, garbled AST statements, or AST nodes containing text offsets from a previous file. | `src/breezeai_cog/parsers/treesitter.py:55` in `parse_source(language, source, timeout_micros)`; `parser.reset()` is at line 68 | `parsers.md` |
| Node 24 native tree-sitter binding failure during onboarding: Silent failure or process termination with zero captures emitted when running repository onboarding via `/breeze:onboard-repository`. | Onboarding toolchain integration; documented in `context/09-integrations-and-async.md` | `parsers.md` |
| Groovy parser emits shallow skeleton captures: Groovy source files produce class/method names but omit fine-grained statement graphs or detailed endpoint parameters. | `_load_dekobon_groovy` at `src/breezeai_cog/parsers/treesitter.py:27`, `_EXTERNAL_GRAMMARS` at `src/breezeai_cog/parsers/treesitter.py:43`, and `src/breezeai_cog/parsers/groovy/` | `parsers.md` |
| Unrecognized framework routes or annotations: Controller classes appear in capture, but specific route handlers or HTTP methods are missing from `FileRecord.handlers`. | Framework parser detectors (e.g. `parsers/java_springboot/`, `parsers/csharp_aspnet/`, `parsers/typescript_express/`) | `parsers.md` |
