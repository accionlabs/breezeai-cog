---
title: "breezeai-cog — Domain Core Model"
date: 2026-10-30
---

Scope: Parse pipeline, parser anatomy, tree-sitter mechanics, emit/NDJSON stage, detection, how to add a parser.

## Parse pipeline (end to end)

```
1. scan(repo_root, classifier, engine, ...)       src/breezeai_cog/core/scanner.py
   → ScanEntry list (path, language); a shared file (.h) gets the language
     core/ownership.resolve_owners decides: content proof, references, neighbours/repo, default
2. build_index(repo_root, entries, jobs)           src/breezeai_cog/core/pipeline.py:158
   → per-language index (FQCN maps, TS aliases, Angular mounts, ...)
3. executor.parse_entries(entries, ...)            src/breezeai_cog/core/executor.py:221
   → ProcessPoolExecutor(spawn); workers call _parse_entry per file
4. _parse_entry(path, repo_root, options)          src/breezeai_cog/core/executor.py:84
   a. read bytes from disk
   b. select(path, source, ..., owner) → single parser (claims + priority), limited to the owner language for a shared file
   c. parser.parse_file(ParseContext) → FileRecord; ctx.related_indexes holds the indexes of the base's related_languages
   d. parser.link_handlers(record, ctx)            (optional post-pass)
   e. _prune_template_edges(record, dropped)
5. pipeline._assemble(records, sink, ...)          src/breezeai_cog/core/pipeline.py:280
   → split_oversized_statements, sink.write(record), accumulate meta
6. sink.finalize(meta)                             writes projectMetaData as line 1
```

Two execution modes:
- **Parallel** (`run`): `ProcessPoolExecutor`; batches of ~N/jobs/4 files per future; workers communicate via `Manager().Queue` log listener.
- **Sequential** (`run_inprocess`): used by the server `/api/analyze` path (small file sets, avoid pool startup cost).

## Tree-sitter mechanics

- Grammar pin: `tree-sitter >=0.24,<0.26`. Version 0.26 removed `Parser.timeout_micros` (the only per-file timeout API that works with bytestring input). Migration to chunked-callback source would be required to use 0.26+.
- Groovy grammar: `dekobon-tree-sitter-groovy` from the Accionlabs fork `groovy-grammar-upstream` branch — adds nested types, modifier enums, enum members without `;`, enhanced-for, range, diamond-operator, untyped-catch. The language-pack `groovy` grammar is a "command soup" with no structural nodes.
- Each worker process loads grammars **lazily** and caches them per process (no cross-process sharing).
- Parse timeout: `parse_timeout_micros` (converted from `settings.parse_timeout` in seconds) is set on the `Parser` via `Parser.timeout_micros`. A timeout raises `ValueError` inside `parse_file`; it is caught by `_parse_entry`'s per-file exception guard.

## Parser anatomy

### LanguageParser Protocol

Source: `src/breezeai_cog/parsers/base.py:92`

| Attribute / method | Required | Description |
|---|---|---|
| `name: str` | ✓ | Language / framework name |
| `extensions: tuple[str, ...]` | ✓ | File extensions this parser handles |
| `schema_version: str` | ✓ | Must equal `SCHEMA_VERSION` (checked at registration) |
| `statement_types: list[str]` | ✓ | Statement types this parser emits |
| `frameworks: list[str]` | ✓ | Framework names (for capabilities output) |
| `parse_file(ctx) -> FileRecord` | ✓ | Main parse method |
| `priority: int` | optional | 0 = base language fallback; >0 = framework parser |
| `template_extensions: tuple[str, ...]` | optional | Markup extensions gated by `--capture-templates` |
| `build_index(repo_root, files, jobs) -> Any` | optional | Repo-level pre-pass for cross-file resolution; gets `parse_timeout_micros` too when `build_index_accepts_timeout` is set |
| `related_languages: tuple[str, ...]` | optional | Languages whose indexes this parser may read (C and C++) |
| `ownership: Ownership \| None` | optional | Evidence for the ownership step when the language shares an extension with another |
| `link_handlers(record, ctx)` | optional | Post-parse: resolve handler locations cross-file |
| `claims(path, source) -> bool` | optional | Override to sniff framework signature; base returns `True` |
| `matches(path) -> bool` | optional | Extension/filename candidacy gate; base uses `extensions` |

### ParseContext fields

Source: `src/breezeai_cog/parsers/base.py:55`

| Field | Notes |
|---|---|
| `path: str` | Repo-relative path |
| `abs_path: Path` | Absolute path |
| `source: bytes` | File bytes (read by worker, cached per process) |
| `repo_root: Path` | |
| `capture_statements: bool` | Whether to emit in-body statements |
| `statement_text_limit: int` | Max chars per statement before splitting |
| `parse_timeout_micros: int` | Tree-sitter timeout; 0 = none |
| `resolution_index: Any \| None` | Result of `build_index` for this language |
| `related_indexes: Mapping[str, Any]` | Indexes of the parser's `related_languages` built for this run, by language name; empty when none |

### Additive detectors

`parsers/additive.py` — language-neutral statement detectors that run **after** the primary parser's `parse_file`. They can inject additional `Statement` records into the `FileRecord` without replacing the parser (e.g. an API-call detector that works across languages). Registered via `DETECTORS` dict in the module and exposed through `all_detectors()` and `fact_sources()`.

### Parser framework families

| Family shard | Parsers |
|---|---|
| JVM | `java`, `java_springboot`, `java_jaxrs`, `java_vertx`, `kotlin`, `kotlin_ktor`, `scala`, `scala_play`, `scala_akkahttp`, `scala_http4s`, `groovy`, `groovy_vertx` |
| .NET | `csharp`, `csharp_aspnet`, `csharp_graphql`, `csharp_hotchocolate`, `csharp_wcf`, `csharp_webforms`, `razor`, `vb`, `vb_aspnet`, `dotnet_servicehost` |
| JS/TS | `typescript`, `typescript_react`, `typescript_angular`, `typescript_vue`, `typescript_nestjs`, `typescript_nextjs`, `typescript_express`, `typescript_fastify`, `typescript_graphql`, `typescript_graphql_codefirst`, `typescript_loopback`, `html` |
| Python | `python`, `python_fastapi` |
| Ruby | `ruby`, `ruby_rails`, `ruby_grape`, `ruby_sinatra` |
| Other | `c`, `cpp` (sharing `cfamily_common`), `graphql`, `prisma`, `hcl`, `hcl_terraform`, `structured_json`, `config` (Dockerfile, package.json, etc.) |

**Groovy status**: skeleton-only. The grammar parses real Groovy structure, but the parser captures class/method outlines only — detailed statement analysis is not yet implemented. See `parsers/groovy/__init__.py` for what is and isn't captured.

## Emit / NDJSON stage

Source: `src/breezeai_cog/emit/`

- `split.py:split_oversized_statements` — splits `Statement.text` exceeding `statement_text_limit` into ordered `#partNofN` records; each flagged `isPartial=True`. Called by `_assemble` before `sink.write`.
- `FileSink` — writes `projectMetaData` to a temp file first (so line 1 is always correct), then streams `FileRecord`s; on `finalize` replaces temp with header. Produces `<repo>-project-analysis.ndjson.gz`.
- `S3Sink` — streams to AWS S3 multipart upload; used by `analyze-diff` server route. Never calls `finalize` from the route — the route calls `upload.close()` separately.

## Detection pipeline (statements)

When `capture_statements=True`:
1. The parser's `parse_file` walks the tree-sitter AST and builds statements with helpers from `statements_common` (for example `classify_statement` and `member_statement`).
2. `parsers/statements_common.py` houses cross-language route/API call pattern matching: fold-concat for `+`-joined URL strings (bounded by `max_concat_depth`).
3. `parsers/callresolve.py` resolves call targets to in-repo file paths (builds `Call.path`).
4. `parsers/index_common.py` / `parsers/enclosing.py` — shared index building and enclosing-scope lookup utilities.
5. Additive detectors (`parsers/additive.py`) inject further statements post-parse.

## Scanner

Source: `src/breezeai_cog/core/scanner.py`

`scan(repo_root, classifier, engine, max_file_size, skip_rule, on_skip)` walks the repo via `os.walk`, applies ignore layers in order, yields `ScanEntry(path, language)`. Files larger than `max_file_size` (default 2 MB) are skipped with `reason="oversized"`.
