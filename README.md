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
- **[Gap Review](skills/gap-review/SKILL.md)** — the procedure an engineer or agent runs to apply that guide, with a capture-check script.
- **[Template Capture](docs/template-capture.md)** — how `.html` / `.vue` / `.cshtml` / `.aspx` are handled, and why they are skipped by default.

## Supported languages & frameworks

`breezeai-cog capabilities` prints the authoritative, live list. Snapshot:

| Language | Extensions | Framework / detector support |
|---|---|---|
| TypeScript / JavaScript | `.ts .tsx .mts .cts .js .jsx .mjs .cjs` `.vue` ‡ | NestJS (& routing-controllers), Angular, AngularJS 1.x (ngRoute and ui-router routes, controller/component/directive registrations, `$resource`; additive), Express, React, Vue, Next.js (App + Pages Router API routes), LoopBack, GraphQL (schema-first + code-first + an in-house resolver framework, server + client ops); AWS SNS/SQS/EventBridge/Lambda/S3/SES/CloudFront/Kinesis/DynamoDB/API Gateway/Cognito/SSM Parameter Store (additive); HubSpot/Chargebee/Salesforce SDKs (additive) |
| Python | `.py` | FastAPI; Flask (additive — routes, `add_url_rule`, `MethodView`, blueprint prefixes followed across files, `before_request` hooks and Flask-Login as guards) |
| PHP | `.php .phtml` | Laravel, CodeIgniter 4, Slim, Symfony; WordPress hooks, Laravel Event/Bus and Symfony EventDispatcher/Messenger (additive) |
| Ruby | `.rb` | Rails (**NEW in the Python target**), Sinatra (**NEW in the Python target**), Grape (**NEW in the Python target**) |
| Java | `.java` `struts.xml` | Spring Boot (incl. `@Scheduled` / `@Schedules` and `TaskScheduler` / `ScheduledTaskRegistrar` timers, Kafka / RabbitMQ / JMS / SQS listeners and Spring Cloud Stream `@StreamListener`, Spring Security URL rules as route guards), JAX-RS, Struts2 (`struts.xml` actions and its includes, Convention-plugin `@Action` / `@Namespace` annotations and un-annotated Convention actions; 2.0 – 7.x), Vert.x 2 – 4 (routes, sub-routers, EventBus, service proxies, timers; Vert.x 2 registration tables, address helpers and `HttpClient` calls; additive, so also in Spring / JAX-RS classes) |
| C# | `.cs .asmx .svc` `.cshtml .razor .aspx .ascx .master` ‡ | ASP.NET (MVC / Web API / Minimal API / Web Forms), Razor Pages (`.cshtml`), Blazor (`.razor`), WCF / ASMX (SOAP), .NET ServiceHost, GraphQL (graphql-dotnet + HotChocolate — attribute, type-extension and fluent-descriptor styles, incl. field resolvers and subscription topics); Lucene.NET index reads/writes (additive) |
| VB.NET | `.vb` | ASP.NET |
| Kotlin | `.kt` | Ktor |
| Go | `.go` `go.mod` `go.sum` | net/http (`ServeMux`, Go 1.22 patterns), Gin, Echo, chi, Fiber, gorilla/mux, gRPC services from generated `ServiceDesc`, NATS, Kafka (kafka-go / sarama / confluent), robfig/cron and `time` tickers; AWS Lambda handlers (`lambda.Start`, trigger from the event type); database access by receiver type — database/sql, sqlx, GORM, pgx, MongoDB, go-redis (additive) — group / mount prefixes followed across files |
| Scala | `.scala .sc .mill` | Play (P2), Akka / http4s / Spark (P3); outbound HTTP calls through a typed Play WS, http4s, sttp, akka-http, Spring RestTemplate or java.net.http client, and scalaj `Http(…)` |
| Apex | `.cls .trigger` | REST resources (`@RestResource`), Lightning (`@AuraEnabled`), SOAP `webservice`, triggers (record changes, Platform Events, Change Data Capture), scheduled Apex (`System.schedule`); SOQL / SOSL / DML, HTTP callouts and `EventBus.publish` |
| C++ | `.cpp .cc .cxx .c++ .hpp .h .hh .hxx .inl .ipp` | — |
| C | `.c .h` (`.h` shared with C++: each header goes to the language that includes it) | — ; calls between C and C++ resolve (C → C++ only through `extern "C"`) |
| CUDA | `.cu .cuh` | — ; kernels and `__device__` functions are functions, a `<<<…>>>` launch is a call; calls into C and C++ resolve |
| JSP | `.jsp .jspx .jspf .tag .tagx` ‡ | JSP 1.0 – Jakarta Pages 4.0, standard and XML syntax: directly served pages as `page` routes, links and Struts tags as navigation; directives, standard actions, JSTL / custom tags, EL and deferred EL; `<%! %>` methods as Functions and scriptlet code as Java statements (JDBC in a scriptlet is a db call); includes, preludes / codas, tag files, `.tld` descriptors and `page import` classes as `importFiles`; declared legacy encodings decoded |
| Groovy † | `.groovy` | Vert.x |
| HTML | `.html .htm` ‡ | Framework-neutral template parser — detects Angular 2+ (grammar-parsed), AngularJS, Vue, Thymeleaf from `templateUrl` resolution or markup fingerprints; htmx / Alpine.js / Stimulus carried as `behaviors` |
| GraphQL | `.graphql .gql .graphqls` | Schema-first SDL — types, fields, operations and resolvers |
| HCL / Terraform | `.tf .tfvars .hcl` | HCL (HashiCorp Configuration Language) — top-level blocks as statements; `module` blocks as Classes (with `constructorParams` for input variables); module and provider sources as `externalImports`; Terraform-specific detection via `hcl_terraform` |
| Prisma | `.prisma` | Prisma Schema Language — `model` → `data_model` entity, `enum` / `datasource` / `generator` blocks (full body on `text`) |
| XSD | `.xsd` | XML Schema — global `element` / `complexType` → `data_model` entity (`endpoint`/`framework` set, full body on `text`); `simpleType` / `group` / `attributeGroup` captured the same way but structural-only (reusable fragments, not entities); `import` / `include` recorded; a relative `schemaLocation` in the repo → `importFiles`, a URL → `externalImports` |
| WSDL | `.wsdl` | WSDL 1.1 (SOAP) — `message` → `data_model` entity; every `portType` operation → a plain statement with its `PortType/Operation` address and DTOs (wiring, not an entry point: the implementing code is the binding), independent of whether a `binding` implements it; `wsdl:import` / embedded-schema locations → `importFiles` / `externalImports`; reuses the XSD walker for embedded `<types>` schemas |
| JSLT | `.jslt` | JSON query/transform language — hand-rolled structural scan (no tree-sitter grammar): `def` → `Function` (untyped params, same-file `calls[]` resolved), `import` / `let` declarations plus the trailing module expression |
| Structured JSON / data | `.json` (+ YAML/TOML config) | Whole-document capture as a TOON `structured_data` statement |
| Config | `package.json`, `tsconfig`, `Dockerfile`, `docker-compose`, `pom.xml`, `requirements.txt`, `build.gradle`, `.csproj` / `.vbproj` / `.sln`, `Makefile`, … | — |

‡ **Markup/view files are skipped by default.** `.html .htm .cshtml .razor .aspx .ascx .master
.vue .jsp .jspx .jspf .tag .tagx` are gated behind `--capture-templates`, because markup produces a large number of low-value
nodes that bury the business logic when reading the graph. They are reported under their own
`template` skip reason, not `ignored`, and `.repoinclude` does not re-include them. Turn the flag
on for Razor Pages / Blazor (`@page` routes and `@code` methods live in the markup) and for Vue
SFC `<script>` blocks and JSP pages. Full details: [Template Capture](docs/template-capture.md).

† **Groovy is best-effort / second-tier.** It reliably captures the package / import /
class / interface / enum / trait / method / field skeleton, nested types included. The
grammar ([dekobon-tree-sitter-groovy](https://github.com/dekobon/tree-sitter-groovy), pinned to
a fork carrying fixes still under upstream review) has known gaps inside method bodies and
enums. A declaration whose header fails to parse is dropped rather than emitted, but these
shapes still give a wrong statement record:

| Code | What is captured |
|---|---|
| `// note` then `String name = load()` | two statements, `String` and `name = load()`; the `load` call is kept |
| `Map<String, Object> errors` (generic or array type, no initializer) | one declaration statement that also holds the next line |
| `(long) (y)` | the cast is read as a call, and the statement can run on into the next line |
| `A { … },` (an enum constant with a body) | the constants after it are lost; they can surface as a field |
| `println name`, `log.info "x"` (a call without parentheses) | no call; the line becomes separate statements. Only a bare name with literal or closure arguments (`println "x"`) parses as a call, and cog does not record it yet |

**Target Spec §2.4 note:** `rails`, `sinatra`, and `grape` are emitted framework values and are
new in the Python target. The local capture schema accepts open framework strings; the external
backend allow-list must add these values before ingestion can rely on them being retained.

PHP framework values (`laravel`, `codeigniter`, `slim`, `symfony`, and `wordpress`) are also
emitted as open framework strings. WordPress action/filter registrations use
`semanticType: "eventbus_consumer"` rather than appearing as HTTP routes. Blade views
(`.blade.php`) remain unsupported and are not classified as PHP source.

PHP `.phtml` files are treated as markup templates and are skipped unless
`--capture-templates` is enabled.

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
