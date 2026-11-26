---
title: "breezeai-cog — Diagnosis: Core and Schemas"
date: 2026-10-30
---

Scope: Failure modes, assumptions, blast radius, and fixes for core pipeline, executor, scanner, ignore rules, and schemas.

## Schema version mismatch on parser registration

- **Symptom:** `RegistryError: parser '<name>' schema_version 'X' != '2.4'` raised during boot or parser discovery.
- **Invariant / Assumption:** `SCHEMA_VERSION` (`src/breezeai_cog/schemas/version.py:20`, currently `"2.4"`) is what every parser class registered in `_REGISTRY` must declare as `schema_version`.
- **Origin site:** `src/breezeai_cog/core/registry.py:29` in `register(parser)` (raises `RegistryError`).
- **Blast radius:** Startup aborts; neither CLI commands nor FastAPI HTTP server will boot.
- **Fix approach:** Ensure the parser class defines `schema_version = SCHEMA_VERSION` imported from `breezeai_cog.schemas.version`. Do not hardcode version strings.

## Per-file parse crash or tree-sitter timeout

- **Symptom:** Log warning `"parse.file.failed"` with `path=<path>`, `parser=<name>`, and `error=<msg>`. File is missing from output NDJSON.
- **Invariant / Assumption:** Individual file parse errors or timeouts are isolated; a crash on one malformed file must never abort repo-wide extraction.
- **Origin site:** `src/breezeai_cog/core/executor.py:84` in `_parse_entry(path, repo_root, options)`; the `except Exception` guard logs the warning and returns `None`. The per-language index is read from `options["indexes"]`.
- **Blast radius:** Exactly that single file record is dropped from the resulting NDJSON capture contract; all other files parse normally.
- **Fix approach:**
  1. If `error="Parsing failed"`: tree-sitter timeout was reached. Raise `parse_timeout` (seconds, default 10, 0 disables; env `BREEZEAI_COG_PARSE_TIMEOUT`). `_options` in `core/executor.py` converts it to `options["parse_timeout_micros"]`. There is no CLI flag for it.
  2. If AST traversal raised `KeyError` or `AttributeError`: the grammar or parser node visitor encountered an unexpected AST shape. Reproduce on a small directory containing the file with `breezeai-cog repo-to-json-tree --repo <dir> --out <out> --jobs 1 --verbose` (there is no single-file option) and update the node visitor in the language parser.

## Cross-file resolution index failure

- **Symptom:** Log warning `"build_index.failed"` with `parser=<name>` and `error=<msg>`. Cross-file calls remain unresolved.
- **Invariant / Assumption:** Index build failure degrades gracefully to "no index for this language" (honest-null); extraction continues without cross-file linkage.
- **Origin site:** `src/breezeai_cog/core/pipeline.py:158` in `_build_indexes(repo_root, entries, jobs, *, debug_on, parse_timeout_micros)`; the `except Exception` around `base.build_index` logs the warning.
- **Blast radius:** Cross-file symbol resolution for that specific language is disabled (`index = None`). Handlers and statements are still captured, but `Call.path` stays unset for cross-file calls.
- **Fix approach:** Inspect index builder implementation in `parsers/<lang>/index.py`. Check if a file in the project has unhandled character encoding or circular inheritance.

## Expected file missing from scan results

- **Symptom:** A source file exists in the repo but is never processed by any parser and does not appear in NDJSON output.
- **Invariant / Assumption:** Files are skipped if they:
  1. Match patterns read per directory by `read_dir_ignore_spec` (`src/breezeai_cog/core/ignore.py:54`) from `.repoignore` and `.gitignore`, unless re-included by `.repoinclude` (`read_dir_include_spec`, `ignore.py:64`).
  2. Match the built-in patterns in `core/default_ignores.txt` (`builtin_ignore_lines`, `ignore.py:43`), which prune directories such as `node_modules` during the walk.
  3. Exceed `max_file_size` (`Settings.max_file_size`, default 2,000,000 bytes).
  4. Have an extension no registered parser claims (the `classify` callback returns `None`; recorded as `unsupported`).
  5. Be a markup/view template while `--capture-templates` is off (recorded as `template`; `.repoinclude` does not override it).
- **Origin site:** `scan` and its inner `keep` in `src/breezeai_cog/core/scanner.py:67`; skip reasons are tallied by `SkipReport` in `src/breezeai_cog/core/skips.py:33`.
- **Blast radius:** Missing scan entry; no records emitted for the file.
- **Fix approach:** Verify the file is under `max_file_size`; verify a parser claims its extension (`breezeai-cog capabilities`); check `.repoignore`, `.gitignore` and `.repoinclude` for over-broad patterns. The skip report written by the CLI lists each skipped file with its reason.
