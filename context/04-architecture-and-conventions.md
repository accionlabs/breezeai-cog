---
title: "breezeai-cog — Architecture and Conventions"
date: 2026-10-30
---

Scope: Layering, dependency direction, conventions, recipes for adding a parser.

## Layering

```
cli.py / server/routes.py      ← thin clients; no business logic
       ↓
services/                      ← orchestration (AnalysisService, upload, diff)
       ↓
core/pipeline.py               ← scan → resolve_owners → build_index → parse_entries → assemble
       ↓
core/executor.py               ← ProcessPoolExecutor (spawn context)
       ↓
parsers/<lang>/                ← one parser per file; returns FileRecord
                                  (parsers/cfamily_common/ holds the extraction C and C++ share)
       ↓
schemas/capture.py             ← Pydantic models (source of truth for NDJSON contract)
       ↓
emit/                          ← statement splitting, NDJSON serialization
       ↓
infra/                         ← FileSink / S3Sink (storage-agnostic write)
```

Dependencies flow downward only. Parsers never import services; services never import the CLI.

## Parser selection model

A file is parsed by **exactly one** parser:
1. `discover_builtin()` imports every `parsers/<lang>/__init__.py` and registers parsers via `PARSERS` list.
2. `parsers_for(path)` returns all registered parsers whose `matches(path)` is True (extension / filename).
3. `select(path, source)` filters to those whose `claims(path, source)` is True, then picks the **highest-priority** one.
4. Base language parser has `priority=0` (fallback). Framework parsers have `priority>0` and sniff their framework's signature in `source` bytes.
5. An extension that several base languages register is **shared** (`.h`: C and C++). `core/ownership.py` decides each shared file's language once, after the scan, from the whole repo.
   `select(path, source, parse_timeout_micros, language)` and `base_parser_for(path, language)` then consider only that language's parsers. C and C++ share extraction in `parsers/cfamily_common/`; a language lists the languages whose indexes it may read in `related_languages`, delivered as `ctx.related_indexes`.

## Key contracts

| Contract | Location |
|---|---|
| `LanguageParser` (Protocol) | `src/breezeai_cog/parsers/base.py:92` |
| `BaseParser` (convenience base) | `src/breezeai_cog/parsers/base.py:105` |
| `ParseContext` (dataclass) | `src/breezeai_cog/parsers/base.py:55` |
| `FileRecord` (Pydantic model) | `src/breezeai_cog/schemas/capture.py:176` |
| `Settings` (pydantic-settings) | `src/breezeai_cog/config.py:37` |

## Error conventions

- Per-file parse failures are **isolated** in `executor._parse_entry`: any exception is caught, logged as `parse.file.failed`, and `None` is returned; the file is counted as "failed" in `projectMetaData` but the run continues.
- `ApiError` (server) maps to `{"error": "<message>", "failedStep?": "..."}` JSON; `RequestValidationError` returns 400 (not FastAPI's default 422).
- Upload failures: 4xx from the backend are fatal (no retry). 5xx / network errors retry up to `upload_max_retries`.

## How to add a new parser

1. Create `src/breezeai_cog/parsers/<lang>/` with `__init__.py` exporting `PARSERS: list[LanguageParser]`.
2. Subclass `BaseParser`; set `name`, `extensions`, `schema_version = SCHEMA_VERSION`, `statement_types`, `frameworks`.
3. Implement `parse_file(ctx: ParseContext) -> FileRecord`.
4. For framework detection: override `claims(path, source) -> bool` and set `priority > 0`.
5. For cross-file resolution (imports, call-graph): implement `build_index(repo_root, files, jobs) -> Any`, store in `ctx.resolution_index`. Set `build_index_accepts_timeout = True` when `build_index` also takes `parse_timeout_micros`. For an extension another language registers, set `ownership = Ownership(...)` (`parsers/base.py`).
6. For markup/view files that should be gated by `--capture-templates`: set `template_extensions = (".html", ...)`.
7. Add `ignore.txt` (per-language default ignores) and/or `include.txt` (force-includes) as sibling files.
8. See `docs/DEVELOPER_GUIDE.md`, `docs/parser-reference.md`, `docs/parser-review-guide.md`, and `skills/extend-capture/` for the full recipe. **Docs are intent; code is truth.**
9. Tests go in `tests/unit/parsers/<lang>/`; the test runner is `uv run pytest`.

## Serialization convention

Serialize `FileRecord` with `model_dump_json(by_alias=True, exclude_none=True)`:
- `by_alias=True` so `__type` emits correctly.
- `exclude_none=True` so route/db-only fields that are `None` are absent in output.
- Collection fields (`functions`, `classes`, `statements`, etc.) default to `[]` and may appear empty.

## Statement splitting

Statements whose `text` exceeds `statement_text_limit` (default 8000 chars) are split by `emit/split.py` into ordered `#partNofN` sibling records, each flagged `isPartial=True`. The backend reassembles them. This is lossless unless `max_statement_parts` cap drops the tail.

## Ignore / include layers

1. **Global built-ins** (`core/ignore.py`, `IgnoreEngine`) — universal ignores (`.git`, `node_modules`, etc.)
2. **Per-language defaults** — `parsers/<lang>/ignore.txt` (layer 2)
3. **Per-language force-includes** — `parsers/<lang>/include.txt` (override layer 2)
4. **Per-repo** — `.gitignore`, `.cogignore`, `.repoignore`, `.repoinclude` read at scan time
5. **Request-level** — `ignorePatterns` in `analyze-diff` request body; appended by `core/ignore.append_repo_ignore_patterns`

Templates (markup files) are a separate gate: dropped at scan time by `pipeline._template_skip_rule` unless `capture_templates=True`.
