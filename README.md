# breezeai-cog

Python code-ontology generator — parses source repositories into the **capture NDJSON contract
(Part C)** consumed by the Breeze backend (Neo4j graph + embeddings) and MCP.

Python reimplementation of `breezeai-code-ontology-generator`.

## Documentation

- **[User Guide](docs/USER_GUIDE.md)** — install, CLI usage, output format, configuration, and the HTTP service.
- **[Developer Guide](docs/DEVELOPER_GUIDE.md)** — setup, project layout, and how it works.
- **[Adding a Parser](docs/ADDING_A_PARSER.md)** — add a new language or framework parser.
- **[Architecture](../../breeze-cog/docs/ARCHITECTURE.md)** — the full design and rationale.

## Status

Parses Python, TypeScript/JavaScript, and Java — with FastAPI, NestJS, Angular, and Spring Boot
framework detection — into the Part C capture contract, and serves the analysis API.

- `src/breezeai_cog/schemas/` — the Part C capture contract as Pydantic v2 models
  (**source of truth**). `export_json_schema()` generates the language-agnostic JSON Schema for
  cross-language consumers; `SCHEMA_VERSION = 2.0`.
- `core/`, `parsers/`, `emit/`, `services/`, `server/` — the scanner, parser registry, multiprocess
  pipeline, NDJSON/S3 sinks, and the FastAPI service (`/api/analyze[-diff|-sql|-es]`).

## Develop

```bash
uv sync --extra all      # runtime + server extras + dev tools
uv run pytest            # test
uv run ruff check . && uv run mypy
```

See the [Developer Guide](docs/DEVELOPER_GUIDE.md) for the project layout and how to add a parser.
