---
title: "breezeai-cog — Symbol Reference: Parser Framework"
date: 2026-10-30
---

Scope: Shared parser infrastructure — `parsers/treesitter`, `parsers/detection/`, `parsers/statements_common`, `parsers/callresolve`, `parsers/index_common`, `parsers/enclosing`, `parsers/emits`, `parsers/wiring`, `parsers/contract`, `parsers/additive`.

## parsers/treesitter.py

Tree-sitter grammar management — lazy loading, cached per process.

| Symbol | Anchor | Notes |
|---|---|---|
| `_load_dekobon_groovy()` | `src/breezeai_cog/parsers/treesitter.py:27` | Loads the Groovy grammar from `dekobon_tree_sitter_groovy` (the pinned Accionlabs fork) |
| `_EXTERNAL_GRAMMARS` | `src/breezeai_cog/parsers/treesitter.py:43` | `{language: loader}` for grammars not in the language pack; holds only `groovy` |
| `get_parser(language)` | `src/breezeai_cog/parsers/treesitter.py:49` | Cached `tree_sitter.Parser`; uses `_EXTERNAL_GRAMMARS` first, else the language pack |
| `parse_source(language, source, timeout_micros=0)` | `src/breezeai_cog/parsers/treesitter.py:55` | Resets the cached parser, sets `timeout_micros` (0 disables), parses bytes; a timeout raises `ValueError` ("Parsing failed") |
| `node_text(node, source)` | `src/breezeai_cog/parsers/treesitter.py:75` | Decoded text of a node |
| `line_span(node)` | `src/breezeai_cog/parsers/treesitter.py:79` | 1-based `(start_line, end_line)` of a node |
| `first_line(text)` | `src/breezeai_cog/parsers/treesitter.py:84` | First line of a text |
| `entry_text(node, source, nested=("children",))` | `src/breezeai_cog/parsers/treesitter.py:92` | Statement text capped at `ENTRY_TEXT_LIMIT` (240) |

## parsers/detection/

Language-neutral call classifiers shared by every language parser.

| Symbol | Anchor | Notes |
|---|---|---|
| `classify_call(callee, method, arg, language, typed_db_ids, http_client_ids)` | `src/breezeai_cog/parsers/detection/__init__.py:13` | Returns `(semanticType, method, dataAccessHint)` for `api_call`, `query_statement` or `db_method_call`, else `None` |
| `match_api(...)` | `src/breezeai_cog/parsers/detection/api_calls.py:73` | HTTP client call detection; returns the verb or `None` |
| `match_db(callee, method, language, ...)` | `src/breezeai_cog/parsers/detection/db_queries.py:308` | ORM/DB method detection; returns a data-access hint or `None` |
| `is_query(method, arg)` | `src/breezeai_cog/parsers/detection/queries.py:44` | True when the call carries a raw SQL string |
| `text_has_query(text)` | `src/breezeai_cog/parsers/detection/queries.py:67` | True when free text looks like SQL |

`detection/sdk_calls.py` holds SDK-client detection (outbound calls through known SDKs).

## parsers/statements_common.py

Cross-language statement helpers.

| Symbol | Anchor | Notes |
|---|---|---|
| `set_http_client_ids(ids)` | `src/breezeai_cog/parsers/statements_common.py:54` | Sets the per-file HTTP-client name set; returns a token |
| `reset_http_client_ids(token)` | `src/breezeai_cog/parsers/statements_common.py:60` | Restores the previous set |
| `current_http_client_ids()` | `src/breezeai_cog/parsers/statements_common.py:64` | Names active for the file being extracted |
| `begin_concat_tracking()` | `src/breezeai_cog/parsers/statements_common.py:69` | Starts collecting skipped-concat lines for the current file |
| `summarize_skipped_concats(path)` | `src/breezeai_cog/parsers/statements_common.py:74` | One warning line if any concats were skipped, else `None` |
| `text_with_trailing_comment(node, source)` | `src/breezeai_cog/parsers/statements_common.py:126` | Node text extended over a trailing comment on the same line |
| `url_placeholder(expr_text)` | `src/breezeai_cog/parsers/statements_common.py:137` | Placeholder (`{name}`) for a non-literal URL part |
| `strip_leading_base(url)` | `src/breezeai_cog/parsers/statements_common.py:143` | Drops a leading base-URL segment |
| `set_concat_depth(depth)` | `src/breezeai_cog/parsers/statements_common.py:163` | Overrides the concat nesting cap for this process; ignores falsy or non-positive values |
| `render_concat(node, source, render)` | `src/breezeai_cog/parsers/statements_common.py:187` | Renders a string-concat node to a path; returns `None` past the depth cap |
| `resolve_endpoint(...)` | `src/breezeai_cog/parsers/statements_common.py:206` | Resolves a call's endpoint string |
| `classify_statement(...)` | `src/breezeai_cog/parsers/statements_common.py:286` | Classifies a tree-sitter node into a statement record |
| `member_statement(...)` | `src/breezeai_cog/parsers/statements_common.py:472` | Builds a statement for a class member |
| `emit_enum_members(...)` | `src/breezeai_cog/parsers/statements_common.py:505` | Emits statements for enum members |

## parsers/callresolve.py

| Symbol | Anchor | Notes |
|---|---|---|
| `CallResolver` | `src/breezeai_cog/parsers/callresolve.py:32` | Type alias `Callable[..., str or None]` |
| `make_resolver(bindings, local_defs, path, types, *, ext_index, heritage, builtin_methods)` | `src/breezeai_cog/parsers/callresolve.py:43` | Builds `(name, receiver, owner=None) -> repo path or None`; ambiguous results are `None` |
| `noop_resolver(...)` | `src/breezeai_cog/parsers/callresolve.py:114` | Resolver that never resolves |

## parsers/index_common.py

Shared helpers for per-language `build_index`.

| Symbol | Anchor | Notes |
|---|---|---|
| `seed_same_package(bindings, package, index)` | `src/breezeai_cog/parsers/index_common.py:29` | Binds same-package top-level types to their files |
| `parallel_map(items, fn, jobs=1)` | `src/breezeai_cog/parsers/index_common.py:46` | Ordered process-pool map; serial when `jobs <= 1` |
| `record_distinct(...)` | `src/breezeai_cog/parsers/index_common.py:61` | Stores a value per key; a conflicting second value collapses it to `None` |
| `ClassHeritage` | `src/breezeai_cog/parsers/index_common.py:88` | Dataclass `{extends, decorators, methods}` for one class |
| `record_heritage(...)` | `src/breezeai_cog/parsers/index_common.py:105` | Records one class's heritage under its simple name |
| `merge_heritage(...)` | `src/breezeai_cog/parsers/index_common.py:121` | Merges heritage under a key |
| `project_heritage(by_fqn)` | `src/breezeai_cog/parsers/index_common.py:152` | Projects a by-FQN heritage map |
| `HeritageChain` | `src/breezeai_cog/parsers/index_common.py:179` | Result of walking a class's base chain |
| `walk_heritage(...)` | `src/breezeai_cog/parsers/index_common.py:193` | Walks the base chain of a class |

## parsers/enclosing.py

| Symbol | Anchor | Notes |
|---|---|---|
| `Enclosing` | `src/breezeai_cog/parsers/enclosing.py:55` | Per-detector-run lookups over one `FileRecord`; follows later appends to `record.statements` |
| `Enclosing.statement(line)` | `src/breezeai_cog/parsers/enclosing.py:66` | Smallest captured statement spanning a line |
| `Enclosing.function_id(line, fallback)` | `src/breezeai_cog/parsers/enclosing.py:72` | Id of the smallest function spanning a line, else `fallback` |

## parsers/emits.py

| Symbol | Anchor | Notes |
|---|---|---|
| `Enrich` | `src/breezeai_cog/parsers/emits.py:47` | Literal of enrich modes: `never`, `unclassified`, `own`, `starts`, `same`, `any` |
| `Emit` | `src/breezeai_cog/parsers/emits.py:51` | One statement a detector found: node, `fields`, `fill`, `enrich`, `reclassify`, overrides |
| `apply(record, emits, source, path)` | `src/breezeai_cog/parsers/emits.py:91` | Writes emits onto the record, enriching existing statements or appending new ones |

## parsers/additive.py

Additive detectors run after a language's primary parse and add statements; facts are collected during `build_index`.

| Symbol | Anchor | Notes |
|---|---|---|
| `HOOKED_LANGUAGES` | `src/breezeai_cog/parsers/additive.py:62` | Languages whose parser calls `run_additive` (`typescript`, `csharp`, `java`, `python`, `scala`, `php`, `go`, `c`, `cpp`); `INDEX_HOOKED_LANGUAGES` (line 68) lists those whose `build_index` collects facts, including `c` and `cpp` |
| `DetectContext` | `src/breezeai_cog/parsers/additive.py:77` | Per-file input: `root`, `ctx`, `record`, `is_fixture`, optional `bindings` and `types` |
| `Gate` | `src/breezeai_cog/parsers/additive.py:100` | When a detector runs: `language`, `order`, `guard` bytes, `skip_fixtures`; `admits()` |
| `Detection` | `src/breezeai_cog/parsers/additive.py:120` | Detector result: `emits` and optional file-level `label` |
| `Detector` | `src/breezeai_cog/parsers/additive.py:129` | Frozen dataclass `{name, gate, detect, frameworks}` |
| `FactSpec` | `src/breezeai_cog/parsers/additive.py:147` | Cross-file fact: `name`, `language`, `gate`, `collect`, `resolve`, `sources`, `grammar` |
| `register_detector(detector)` | `src/breezeai_cog/parsers/additive.py:177` | Adds a detector to the registry |
| `register_fact(spec)` | `src/breezeai_cog/parsers/additive.py:197` | Adds a fact spec to the registry |
| `discover_detectors()` | `src/breezeai_cog/parsers/additive.py:214` | Imports modules that register detectors or facts, once |
| `detectors_for(language)` | `src/breezeai_cog/parsers/additive.py:238` | Detectors for a language in run order |
| `all_detectors()` | `src/breezeai_cog/parsers/additive.py:244` | `{language: [Detector, ...]}` copy |
| `facts_for(language)` | `src/breezeai_cog/parsers/additive.py:249` | Fact specs for a language |
| `fact_sources()` | `src/breezeai_cog/parsers/additive.py:255` | `{language: (ext, ...)}` extra extensions facts collect from; used by `_build_indexes` |
| `run_additive(language, root, ctx, record, *, is_fixture, bindings, types)` | `src/breezeai_cog/parsers/additive.py:317` | Runs gated detectors and applies their emits; no-op when statements are not captured |
| `collect_additive(language, root, source, rel, repo_root)` | `src/breezeai_cog/parsers/additive.py:342` | Runs every fact `collect` on one file |
| `resolve_additive(language, collected, index)` | `src/breezeai_cog/parsers/additive.py:358` | Resolves facts once after the index is reduced |
| `index_fact(index, name)` | `src/breezeai_cog/parsers/additive.py:368` | Reads a resolved fact off an index, or `None` |

## parsers/contract.py

Capture-contract checks, run in tests, golden comparisons and under `--verbose`.

| Symbol | Anchor | Notes |
|---|---|---|
| `Violation` | `src/breezeai_cog/parsers/contract.py:83` | Frozen dataclass `{rule, severity, statement_id, detail}`; severity is `error` or `gap` |
| `is_entry_point(s)` | `src/breezeai_cog/parsers/contract.py:103` | True for statements that are entry points |
| `validate(record)` | `src/breezeai_cog/parsers/contract.py:147` | Returns the `Violation` list for a `FileRecord` |

## parsers/wiring.py

Cross-file wiring facts (route prefixes, guards, registrations, hosts).

| Symbol | Anchor | Notes |
|---|---|---|
| `WiringKind` | `src/breezeai_cog/parsers/wiring.py:24` | Literal `prefix`, `guard`, `registration`, `host` |
| `Wiring` | `src/breezeai_cog/parsers/wiring.py:28` | Frozen dataclass `{scope, kind, value, site}` |
| `resolve_wiring(items)` | `src/breezeai_cog/parsers/wiring.py:41` | `{scope: value}`; scopes with conflicting or unresolved values are dropped |
| `compose_guards(*layers)` | `src/breezeai_cog/parsers/wiring.py:50` | Concatenates guard layers outermost first without repeats; `None` when empty |
