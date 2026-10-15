# breezeai-cog

Python code-ontology generator — parses source repositories into the **capture NDJSON contract**
consumed by the Breeze backend (Neo4j graph + embeddings) and MCP.

Python reimplementation of `breezeai-code-ontology-generator`.

## Documentation

- **[User Guide](docs/USER_GUIDE.md)** — install, CLI usage, output format, configuration, and the HTTP service.
- **[Developer Guide](docs/DEVELOPER_GUIDE.md)** — setup, project layout, and how it works.
- **[Architecture](docs/architecture.md)** — how cog works end to end, and the reasoning behind each architectural decision.
- **[Extending Capture](skills/extend-capture/SKILL.md)** — add a new language, framework, or cross-cutting detector (start here); reliability-first discipline.
- **[Parser Reference](docs/parser-reference.md)** — the mechanical step-by-step for building a parser.
- **[Parser Review & Gap Analysis Guide](docs/parser-review-guide.md)** — how to review a parser and write a gap analysis report.
- **[Template Capture](docs/template-capture.md)** — how `.html` / `.vue` / `.cshtml` / `.aspx` are handled, and why they are skipped by default.

## Supported languages & frameworks

`breezeai-cog capabilities` prints the authoritative, live list. Snapshot:

| Language | Extensions | Framework / detector support |
|---|---|---|
| TypeScript / JavaScript | `.ts .tsx .mts .cts .js .jsx .mjs .cjs` `.vue` ‡ | NestJS (& routing-controllers), Angular, Express, React, Vue, Next.js (App + Pages Router API routes), LoopBack, GraphQL (schema-first + code-first + an in-house resolver framework, server + client ops); AWS SNS/SQS/EventBridge/Lambda/S3/SES/CloudFront/Kinesis/DynamoDB/API Gateway/Cognito/SSM Parameter Store (additive); HubSpot/Chargebee/Salesforce SDKs (additive) |
| Python | `.py` | FastAPI |
| Ruby | `.rb` | Rails (**NEW in the Python target**), Sinatra (**NEW in the Python target**), Grape (**NEW in the Python target**) |
| Java | `.java` | Spring Boot, JAX-RS, Vert.x |
| C# | `.cs .asmx .svc` `.cshtml .razor .aspx .ascx .master` ‡ | ASP.NET (MVC / Web API / Minimal API / Web Forms), Razor Pages (`.cshtml`), Blazor (`.razor`), WCF / ASMX (SOAP), .NET ServiceHost, GraphQL (graphql-dotnet + HotChocolate — attribute, type-extension and fluent-descriptor styles, incl. field resolvers and subscription topics); Lucene.NET index reads/writes (additive) |
| VB.NET | `.vb` | ASP.NET |
| Kotlin | `.kt` | Ktor |
| Scala | `.scala .sc` | Play (P2), Akka / http4s / Spark (P3) |
| C++ | `.cpp .cc .cxx .c++ .hpp .h .hh .hxx .inl .ipp` | — |
| Groovy † | `.groovy` | Vert.x |
| HTML | `.html .htm` ‡ | Framework-neutral template parser — detects Angular 2+ (grammar-parsed), AngularJS, Vue, Thymeleaf from `templateUrl` resolution or markup fingerprints; htmx / Alpine.js / Stimulus carried as `behaviors` |
| GraphQL | `.graphql .gql .graphqls` | Schema-first SDL — types, fields, operations and resolvers |
| HCL / Terraform | `.tf .tfvars .hcl` | HCL (HashiCorp Configuration Language) — top-level blocks as statements; `module` blocks as Classes (with `constructorParams` for input variables); module and provider sources as `externalImports`; Terraform-specific detection via `hcl_terraform` |
| Prisma | `.prisma` | Prisma Schema Language — `model` → `data_model` entity, `enum` / `datasource` / `generator` blocks (full body on `text`) |
| Structured JSON / data | `.json` (+ YAML/TOML config) | Whole-document capture as a TOON `structured_data` statement |
| Config | `package.json`, `tsconfig`, `Dockerfile`, `docker-compose`, `pom.xml`, `requirements.txt`, `build.gradle`, `.csproj` / `.vbproj` / `.sln`, `Makefile`, … | — |

‡ **Markup/view files are skipped by default.** `.html .htm .cshtml .razor .aspx .ascx .master
.vue` are gated behind `--capture-templates`, because markup produces a large number of low-value
nodes that bury the business logic when reading the graph. They are reported under their own
`template` skip reason, not `ignored`, and `.repoinclude` does not re-include them. Turn the flag
on for Razor Pages / Blazor (`@page` routes and `@code` methods live in the markup) and for Vue
SFC `<script>` blocks. Full details: [Template Capture](docs/template-capture.md).

† **Groovy is best-effort / second-tier.** It reliably captures the package / import /
class / interface / enum / trait / method / field skeleton, but the grammar
([dekobon-tree-sitter-groovy](https://pypi.org/project/dekobon-tree-sitter-groovy/)) degrades
on some expression bodies (named-argument commas, parenthesised enum constants) and does not
parse **nested type declarations** (a `class`/`enum` inside a class body). Degradation is
always to *missing* nodes, never wrong ones — the parser fabricates nothing it cannot verify.

**Target Spec §2.4 note:** `rails`, `sinatra`, and `grape` are emitted framework values and are
new in the Python target. The local capture schema accepts open framework strings; the external
backend allow-list must add these values before ingestion can rely on them being retained.

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
- **[Architecture](docs/architecture.md)** — how cog works end to end, and the reasoning behind each architectural decision.
