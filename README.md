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
| TypeScript / JavaScript | `.ts .tsx .mts .cts .js .jsx .mjs .cjs` | NestJS (& routing-controllers), Angular, Express, React, Vue, Next.js (App + Pages Router API routes), LoopBack, GraphQL (schema-first + code-first + an in-house resolver framework, server + client ops); AWS SNS/SQS/EventBridge/Lambda/S3/SES/CloudFront/Kinesis/DynamoDB/API Gateway (additive); HubSpot/Chargebee/Salesforce SDKs (additive) |
| Python | `.py` | FastAPI |
| Java | `.java` | Spring Boot, JAX-RS, Vert.x |
| C# | `.cs .asmx .svc` | ASP.NET (MVC / Web API / Minimal API / Web Forms), WCF / ASMX (SOAP), .NET ServiceHost, GraphQL (graphql-dotnet) |
| VB.NET | `.vb` | ASP.NET |
| Kotlin | `.kt` | Ktor |
| C++ | `.cpp .cc .cxx .c++ .hpp .h .hh .hxx .inl .ipp` | — |
| Groovy † | `.groovy` | Vert.x |
| Structured JSON / data | `.json` (+ YAML/TOML config) | Whole-document capture as a TOON `structured_data` statement |
| Config | `package.json`, `tsconfig`, `Dockerfile`, `docker-compose`, `pom.xml`, `requirements.txt`, `build.gradle`, `.csproj` / `.vbproj` / `.sln`, `Makefile`, … | — |

† **Groovy is best-effort / second-tier.** It reliably captures the package / import /
class / interface / enum / trait / method / field skeleton, but the grammar
([dekobon-tree-sitter-groovy](https://pypi.org/project/dekobon-tree-sitter-groovy/)) degrades
on some expression bodies (named-argument commas, parenthesised enum constants) and does not
parse **nested type declarations** (a `class`/`enum` inside a class body). Degradation is
always to *missing* nodes, never wrong ones — the parser fabricates nothing it cannot verify.

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
