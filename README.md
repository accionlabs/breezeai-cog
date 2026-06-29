# breezeai-cog

Python code-ontology generator — parses source repositories into the **capture NDJSON contract
(Part C)** consumed by the Breeze backend (Neo4j graph + embeddings) and MCP.

Python reimplementation of `breezeai-code-ontology-generator`. Architecture:
[`breeze-cog/docs/ARCHITECTURE.md`](../../breeze-cog/docs/ARCHITECTURE.md).

## Status

Milestone **M1 — Skeleton + frozen contract** (in progress).

- ✅ `src/breezeai_cog/schemas/` — the Part C capture contract as Pydantic v2 models
  (**source of truth**). `export_json_schema()` generates the language-agnostic JSON Schema for
  cross-language consumers; `SCHEMA_VERSION = 2.0`.
- ⏳ `config.py`, `logging.py`, `errors.py`, `core/registry.py`, `parsers/base.py`, `emit/`,
  `utils/`, CI.

## Develop

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && mypy
```
