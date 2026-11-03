---
title: "breezeai-cog — Symbol Reference: Core and Schemas"
date: 2026-10-30
---

Scope: `core/`, `schemas/`, `emit/`, `parsers/base`, `parsers/additive`.

## schemas/version.py

| Symbol | Anchor | Notes |
|---|---|---|
| `SCHEMA_VERSION` | `src/breezeai_cog/schemas/version.py:20` | `"2.4"` — all parsers must match at registration |
| `export_json_schema()` | `src/breezeai_cog/schemas/version.py:23` | Generates JSON Schema from Pydantic models; returns `dict` |
| `write_json_schema(path)` | `src/breezeai_cog/schemas/version.py:44` | Writes generated schema to file |

## schemas/capture.py

| Symbol | Anchor | Notes |
|---|---|---|
| `Decorator` | `src/breezeai_cog/schemas/capture.py:35` | `{name, args: list[str]}` |
| `Parameter` | `src/breezeai_cog/schemas/capture.py:42` | `{name, type, decorators, default?}` |
| `ConstructorParam` | `src/breezeai_cog/schemas/capture.py:49` | `{name, type}` |
| `Call` | `src/breezeai_cog/schemas/capture.py:56` | `{name, path?}` — path builds CALLS edge |
| `Statement` | `src/breezeai_cog/schemas/capture.py:62` | Flat statement; two-axis: `nodeType` + `semanticType?` |
| `Function` | `src/breezeai_cog/schemas/capture.py:114` | Has `calls`, `params`, `decorators`; no nested statements |
| `Class` | `src/breezeai_cog/schemas/capture.py:144` | `extra="allow"`; has `constructorParams`, `decorators`, `extends`, `alias`, `implements` |
| `FileRecord` | `src/breezeai_cog/schemas/capture.py:176` | `extra="allow"`; root per-file record |
| `ProjectMetaData` | `src/breezeai_cog/schemas/capture.py:208` | Line-1 record; `has_content()` determines if ontology is worth persisting |
| `ProjectMetaData.has_content()` | `src/breezeai_cog/schemas/capture.py:230` | Returns True if any code, functions, classes, captured data docs, IaC, deps, or Docker |

## parsers/base.py

| Symbol | Anchor | Notes |
|---|---|---|
| `_GLOBAL_FIXTURE_MARKERS` | `src/breezeai_cog/parsers/base.py:42` | `(".test.", ".spec.")` — route-only fixture markers |
| `_GLOBAL_FIXTURE_DIRS` | `src/breezeai_cog/parsers/base.py:49` | `{"mock", "mocks", "__mocks__", "fixtures", "__fixtures__"}` |
| `ParseContext` | `src/breezeai_cog/parsers/base.py:55` | Frozen dataclass: `path, abs_path, source, repo_root, capture_statements, statement_text_limit, parse_timeout_micros, resolution_index, related_indexes` |
| `LanguageParser` | `src/breezeai_cog/parsers/base.py:92` | Runtime-checkable Protocol: `name, extensions, schema_version, statement_types, frameworks, parse_file(ctx)` |
| `BaseParser` | `src/breezeai_cog/parsers/base.py:105` | Convenience base with defaults; subclasses implement `parse_file` |
| `BaseParser.matches(path)` | `src/breezeai_cog/parsers/base.py:137` | Extension/filename candidacy; templates matched case-insensitively |
| `BaseParser.claims(path, source)` | `src/breezeai_cog/parsers/base.py:150` | Framework sniff; base returns `True` (fallback) |
| `Ownership` | `src/breezeai_cog/parsers/base.py:72` | Frozen dataclass of optional evidence hooks for the ownership step: `proves`, `references`, `includes`, `default_for`. Functions must be module-level (pickled to workers) and read bytes only |
| `BaseParser.related_languages` | `src/breezeai_cog/parsers/base.py:129` | Languages whose indexes the parser may read after its own; the pipeline passes them as `ctx.related_indexes` |
| `BaseParser.ownership` | `src/breezeai_cog/parsers/base.py:132` | `Ownership` for a language that shares an extension with another; `None` otherwise |
| `BaseParser.build_index_accepts_timeout` | `src/breezeai_cog/parsers/base.py:135` | When set, the pipeline passes the run's `parse_timeout_micros` to `build_index` |
| `BaseParser.build_index(repo_root, files, jobs)` | `src/breezeai_cog/parsers/base.py:156` | Optional pre-pass; base returns `None` |
| `BaseParser.link_handlers(record, ctx)` | `src/breezeai_cog/parsers/base.py:161` | Optional post-parse; resolves handler locations |
| `BaseParser.is_fixture_file(path)` | `src/breezeai_cog/parsers/base.py:208` | Checks fixture markers and fixture dirs |

## core/registry.py

| Symbol | Anchor | Notes |
|---|---|---|
| `_REGISTRY` | `src/breezeai_cog/core/registry.py:26` | Module-level list of registered parsers |
| `register(parser)` | `src/breezeai_cog/core/registry.py:29` | Validates `schema_version`; raises `RegistryError` on mismatch |
| `clear()` | `src/breezeai_cog/core/registry.py:40` | Empties `_REGISTRY` |
| `registered()` | `src/breezeai_cog/core/registry.py:44` | Returns copy of `_REGISTRY` |
| `parsers_for(path)` | `src/breezeai_cog/core/registry.py:48` | All parsers whose `matches(path)` is True |
| `language_candidates(path)` | `src/breezeai_cog/core/registry.py:53` | Every base language parser at the lowest priority that registers the file; more than one means the file is shared |
| `base_parser_for(path, language?)` | `src/breezeai_cog/core/registry.py:63` | Lowest-priority parser (the base language); `language` is the ownership decision for a shared file, and an unknown one falls back to the first-registered candidate |
| `_of_language(parser, base)` | `src/breezeai_cog/core/registry.py:79` | True for `base` and for its framework parsers (higher-priority subclasses); another base language never matches |
| `template_extensions()` | `src/breezeai_cog/core/registry.py:87` | All markup extensions across all registered parsers |
| `select(path, source, parse_timeout_micros, language?)` | `src/breezeai_cog/core/registry.py:98` | Single-parser selection: `claims + priority`; returns highest-priority claiming parser; with `language`, only that language's base and framework parsers are candidates |
| `capabilities()` | `src/breezeai_cog/core/registry.py:125` | Returns capabilities dict (schemaVersion, languages, extensions, frameworks, statementTypes, additiveDetectors) |
| `discover_builtin()` | `src/breezeai_cog/core/registry.py:142` | Idempotent: imports each `parsers/<lang>` module and registers `PARSERS` list |

## core/pipeline.py

| Symbol | Anchor | Notes |
|---|---|---|
| `_lang_ignored(engine, path, language)` | `src/breezeai_cog/core/pipeline.py:89` | True when the language's layer-2 rules drop the path and no include rule forces it in |
| `_scan_entries(repo_root, settings, report?, jobs)` | `src/breezeai_cog/core/pipeline.py:96` | Returns a list of `ScanEntry`; applies ignore/template gate, then `resolve_owners` labels each shared file; a file is ignored only when every candidate language drops it; with `--language`, competing languages are scanned as evidence and dropped as `unsupported` afterwards |
| `_owners(entries)` | `src/breezeai_cog/core/pipeline.py:153` | Shared file to decided language, passed to workers for parser selection |
| `_build_indexes(repo_root, entries, jobs, debug_on, parse_timeout_micros)` | `src/breezeai_cog/core/pipeline.py:158` | Per-language `build_index`; isolated per-language (failure → None, run continues); a shared file joins its decided language; the parse timeout goes only to a parser with `build_index_accepts_timeout` |
| `iter_records(repo_root, settings)` | `src/breezeai_cog/core/pipeline.py:211` | Sequential in-process streaming; yields `(ScanEntry, FileRecord)` |
| `_ConfigSummary` | `src/breezeai_cog/core/pipeline.py:227` | Aggregates config-file metadata into `projectMetaData.configs` |
| `_assemble(repo_root, records, sink, ...)` | `src/breezeai_cog/core/pipeline.py:280` | Streams records to sink; splits oversized statements; assembles `ProjectMetaData`; logs `analysis.complete` |
| `run(repo_root, settings, sink, progress?, summary_out?)` | `src/breezeai_cog/core/pipeline.py:381` | Full parallel analysis → sink |
| `run_inprocess(repo_root, settings, sink)` | `src/breezeai_cog/core/pipeline.py:422` | Sequential in-process (server `/api/analyze` path) |

## core/ownership.py

Decides the language of a shared file (an extension several languages register, such as `.h` for C and C++) from the whole repo, after the scan and before any index is built. Rules run in order and the first with one answer wins: content proof, references, neighbours then repo, default. Evidence is read as bytes; an ambiguous reference is not evidence.

| Symbol | Anchor | Notes |
|---|---|---|
| `Decision` | `src/breezeai_cog/core/ownership.py:45` | Frozen dataclass `{language, rule}`; `rule` is one of content, references, neighbours, repo, default (logged as `ownership.decided`) |
| `shared_languages(path)` | `src/breezeai_cog/core/ownership.py:52` | Candidate languages of a shared file in registration order; empty tuple when one language registers the extension |
| `competing_languages(languages)` | `src/breezeai_cog/core/ownership.py:59` | Languages sharing an extension with the given ones, so a `--language c` run still reads C++ files as evidence |
| `resolve_owners(repo_root, entries, jobs)` | `src/breezeai_cog/core/ownership.py:129` | Path to `Decision` for every shared entry; unshared entries are read only as evidence |
| `_by_references(...)` | `src/breezeai_cog/core/ownership.py:280` | Rule 2: languages of referring files, followed back through other shared files; a header included from both C and C++ is C |
| `_by_neighbours(...)` | `src/breezeai_cog/core/ownership.py:293` | Rule 3: languages of the files in the same folder, then in the repo |
| `_by_default(path, langs, hints)` | `src/breezeai_cog/core/ownership.py:307` | Rule 4: the candidate declaring the extension its default, else the first registered |

## core/executor.py

| Symbol | Anchor | Notes |
|---|---|---|
| `_SPAWN` | `src/breezeai_cog/core/executor.py:32` | `mp.get_context("spawn")` — cross-platform pool context |
| `_options(settings)` | `src/breezeai_cog/core/executor.py:37` | Builds the options dict for workers (capture_statements, template_extensions, timeouts, ...) |
| `_prune_template_edges(record, dropped)` | `src/breezeai_cog/core/executor.py:59` | Removes `importFiles` and `call.path` for skipped templates |
| `_parse_entry(path, repo_root, options)` | `src/breezeai_cog/core/executor.py:84` | Single-file parse: read → select (with the file's `owners` entry) → parse_file → link_handlers → prune; builds `related_indexes` from the base's `related_languages`; per-file exception guard |
| `_init_worker(log_queue, repo_root, options)` | `src/breezeai_cog/core/executor.py:142` | Pool initializer: discover parsers, load pickled indexes, setup worker logging |
| `_parse_batch(batch)` | `src/breezeai_cog/core/executor.py:158` | Worker function: calls `_parse_entry` for each file in the batch |
| `_worker_options(options)` | `src/breezeai_cog/core/executor.py:175` | Context manager: moves indexes to a temp pickle file to avoid pipe-buffer bottleneck |
| `_start_listener()` | `src/breezeai_cog/core/executor.py:206` | Creates `Manager().Queue` + `QueueListener` for cross-process log funneling |
| `parse_entries(entries, repo_root, settings, indexes?, owners?)` | `src/breezeai_cog/core/executor.py:221` | Parallel (ProcessPoolExecutor) or serial (jobs<=1); yields `(language, FileRecord)` |

## core/scanner.py

| Symbol | Anchor | Notes |
|---|---|---|
| `Classifier` | `src/breezeai_cog/core/scanner.py:33` | Type alias: path to language name or `None` |
| `SkipRule` | `src/breezeai_cog/core/scanner.py:37` | Type alias: path to skip reason or `None` (used for templates) |
| `OnSkip` | `src/breezeai_cog/core/scanner.py:40` | Protocol called once per dropped file or pruned directory with a reason |
| `ScanEntry` | `src/breezeai_cog/core/scanner.py:52` | Frozen dataclass: `path` (repo-relative, POSIX), `language` |
| `scan(repo_root, classify, *, engine, max_file_size, follow_symlinks, skip_rule, on_skip)` | `src/breezeai_cog/core/scanner.py:67` | Walks the repo and yields `ScanEntry`; drops ignored, template, unsupported and oversized files; its inner `keep` applies ignore and include stacks |

## core/ignore.py

| Symbol | Anchor | Notes |
|---|---|---|
| `compile_spec(lines)` | `src/breezeai_cog/core/ignore.py:38` | Compiles gitignore-style lines into a `GitIgnoreSpec` |
| `builtin_ignore_lines()` | `src/breezeai_cog/core/ignore.py:43` | Lines of the bundled `default_ignores.txt` |
| `read_dir_ignore_spec(dir_path)` | `src/breezeai_cog/core/ignore.py:54` | Combined `.repoignore` and `.gitignore` spec for one directory, or `None` |
| `read_dir_include_spec(dir_path)` | `src/breezeai_cog/core/ignore.py:64` | `.repoinclude` spec for one directory, or `None` |
| `IgnoreEngine` | `src/breezeai_cog/core/ignore.py:70` | Built-in global specs plus per-language layer-2 specs (`build`, `is_ignored_global`, `is_included_global`, `is_lang_ignored`, `is_lang_included`) |
| `append_repo_ignore_patterns(repo_root, patterns)` | `src/breezeai_cog/core/ignore.py:119` | Appends request-supplied patterns to the repo ignore file; returns the count |

## core/skips.py

| Symbol | Anchor | Notes |
|---|---|---|
| `SkipReport` | `src/breezeai_cog/core/skips.py:33` | Counts and lists skipped files by reason (`ignored`, `template`, `unsupported`, `oversized`), pruned dirs and unsupported extensions; `record()` adds one |

## emit/split.py

| Symbol | Anchor | Notes |
|---|---|---|
| `split_oversized_statements(statements, limit, max_parts)` | `src/breezeai_cog/emit/split.py:91` | Splits statements whose `text` exceeds `limit` into ordered `#partNofN` records flagged `isPartial=True` |
