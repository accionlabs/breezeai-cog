# breezeai-cog

Python code-ontology generator — parses source repositories into the **capture NDJSON contract**
consumed by the Breeze backend (Neo4j graph + embeddings) and MCP.

Python reimplementation of `breezeai-code-ontology-generator`.

## Documentation

- **[User Guide](docs/USER_GUIDE.md)** — install, CLI usage, output format, configuration, and the HTTP service.
- **[Developer Guide](docs/DEVELOPER_GUIDE.md)** — setup, project layout, and how it works.
- **[Extending Capture](skills/extend-capture/SKILL.md)** — add a new language, framework, or cross-cutting detector (start here); reliability-first discipline.
- **[Parser Reference](docs/parser-reference.md)** — the mechanical step-by-step for building a parser.

## Supported languages & frameworks

`breezeai-cog capabilities` prints the authoritative, live list. Snapshot:

| Language | Extensions | Framework / detector support |
|---|---|---|
| TypeScript / JavaScript | `.ts .tsx .mts .cts .js .jsx .mjs .cjs` | NestJS, Angular, Express, React, LoopBack, GraphQL; AWS SNS/SQS/EventBridge/Lambda (additive) |
| Python | `.py` | FastAPI |
| Java | `.java` | Spring Boot, JAX-RS, Vert.x |
| C# | `.cs` | ASP.NET |
| VB.NET | `.vb` | ASP.NET |
| Config | `package.json`, `tsconfig`, `Dockerfile`, `docker-compose`, `pom.xml`, `requirements.txt`, `build.gradle`, … | — |

## Layout

- `src/breezeai_cog/schemas/` — the capture contract as Pydantic v2 models
  (**source of truth**). The language-agnostic JSON Schema is generated on demand for
  cross-language consumers via `breezeai-cog schema` (`export_json_schema()`); `SCHEMA_VERSION = 2.0`.
- `core/`, `parsers/`, `emit/`, `services/`, `server/` — the scanner, parser registry, multiprocess
  pipeline, NDJSON/S3 sinks, and the FastAPI service (`/api/analyze[-diff|-sql|-es]`).

## Develop

```bash
uv sync --extra all      # runtime + server extras + dev tools
uv run pytest            # test
uv run ruff check . && uv run mypy
```

See the [Developer Guide](docs/DEVELOPER_GUIDE.md) for the project layout and how to add a parser.
