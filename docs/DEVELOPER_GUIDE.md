# Developer Guide

How `breezeai-cog` is built, how to work on it, and how to extend it.

---

## Setup

The project is [uv](https://docs.astral.sh/uv/)-managed.

```bash
uv sync --extra all        # runtime + server/upload extras + dev tools
uv run pytest -q           # run the test suite
uv run ruff check .        # lint
uv run mypy                # type-check
```

Dev tools live in the PEP 735 `dev` group (installed by `uv sync`); the HTTP service / S3 / SQL
deps live in the `server` and `all` optional-dependency extras.

---

## Project layout

```
src/breezeai_cog/
  schemas/        # Pydantic v2 capture contract — the SOURCE OF TRUTH (JSON Schema generated on demand)
  config.py       # Settings: CLI flags ↔ env vars (BREEZEAI_COG_*)
  logging.py      # structlog setup
  core/           # registry · scanner · ignore · pipeline · executor (multiprocess)
  parsers/        # tree-sitter parsers
    base.py       #   BaseParser, ParseContext, the LanguageParser protocol
    treesitter.py #   grammar loading + bounded parse
    detection/    #   shared, language-agnostic API/DB/route classification
    <lang>/       #   language parser (python, typescript, java)
    <lang>_<fw>/  #   framework parser (python_fastapi, typescript_nestjs/angular, java_springboot)
  emit/           # id convention · ndjson · gzip · sinks (file/memory) · s3 streaming
  analyzers/      # non-AST: sql (DDL via sqlglot), es (Elasticsearch mappings)
  services/       # analysis · inprocess · diff · notify
  server/         # FastAPI app · routes · deps · git acquisition · errors
  cli.py          # Typer CLI
```

---

## How it works

```
scan → select parser → parse → emit FileRecord → sink (file / memory / S3)
                                                 → projectMetaData
```

1. **Scan** (`core/scanner.py`) walks the repo, applying hierarchical ignore/include rules
   (`.gitignore`/`.repoignore` + per-language defaults) and size limits.
2. **Select** (`core/registry.py`) — each file is parsed by **exactly one** parser:
   `select(path, source)` returns the highest-`priority` parser whose `claims(path, source)` is
   `True`, falling back to the base language parser. Framework parsers (priority 10) sniff their
   signature (e.g. `b"@nestjs/" in source`); the base parser (priority 0) is the fallback. No file
   is ever parsed twice — multiple frameworks for one language coexist by per-file content.
3. **Parse** (`parsers/<lang>/`) turns the tree-sitter AST into a `FileRecord`. The id convention
   (`emit/ids.py`) assigns deterministic `id`/`parentId`, so framework parsers attach routes to the
   right handler.
4. **Emit** through a sink: the gzipped-NDJSON **file** sink (CLI), the **in-memory** sink
   (`/api/analyze`), or the streaming **S3** upload (`/api/analyze-diff|-sql|-es`).

The pipeline runs parsers across processes (`core/executor.py`, spawn-safe) for the CLI, and
sequentially in-process for the server's small payloads.

---

## Adding a parser

A new **language** or **framework** parser is a self-contained subpackage. The full recipe —
structure, the tools to reuse, the `claims`/`priority` selection model, and testing standards — is
in **[Adding a Parser](ADDING_A_PARSER.md)**.

In short:

- **Language** (new grammar) → `parsers/<lang>/` implementing the full parser; `priority = 0`.
- **Framework** (on an existing language) → `parsers/<lang>_<framework>/` that subclasses the base
  parser, sets `priority` + `claims`, and adds only its detection (single parse, no duplication).
- Register by exporting `PARSERS = [...]` from the subpackage's `__init__.py` (auto-discovered).
- Cross-language API/DB call recognition is shared in `parsers/detection/` — feed it, don't fork it.

---

## The capture contract

`src/breezeai_cog/schemas/` (Pydantic v2) **is the source of truth**. The language-agnostic JSON
Schema is *generated on demand* from the models — `breezeai-cog schema` (or `export_json_schema()`) —
for cross-language consumers; it is not committed. `SCHEMA_VERSION` gates parser registration.
Changing the contract means editing the models; consumers regenerate the schema when they need it.

---

## Testing & quality

- `uv run pytest -q` — unit tests under `tests/unit/` (one per module/parser). Parser tests parse a
  synthetic sample, assert extraction, and validate the record against the JSON Schema.
- Server tests use FastAPI's `TestClient` with **injected fake deps** (`ServerDeps`), so the
  streaming endpoints are covered without AWS or a live backend.
- Keep `ruff` and `mypy` clean. Match surrounding style; tests are expected with new parsers/endpoints.

---

For installation and usage, see the [User Guide](USER_GUIDE.md).
