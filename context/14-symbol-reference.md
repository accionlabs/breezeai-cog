---
title: "breezeai-cog — Symbol Reference (Router)"
date: 2026-10-30
---

Scope: Router. Read the router, then **only the one shard** that covers the question.

| Shard | Modules covered |
|---|---|
| `14-symbols/core-and-schemas.md` | `core/pipeline`, `core/executor`, `core/registry`, `core/ownership`, `core/scanner`, `core/ignore`, `core/skips`, `schemas/capture`, `schemas/version`, `schemas/enums`, `emit/split`, `parsers/base`, `parsers/additive` |
| `14-symbols/services-server-integrations.md` | `services/` (analysis, upload, batch, diff, inprocess, notify), `server/app`, `server/routes`, `server/git_routes`, `server/deps`, `server/git`, `infra/`, `analyzers/`, `integrations/scm/` |
| `14-symbols/parser-framework.md` | `parsers/base`, `parsers/treesitter`, `parsers/detection/`, `parsers/statements_common`, `parsers/callresolve`, `parsers/index_common`, `parsers/enclosing`, shared top-level parser modules |
| `14-symbols/parsers-jvm.md` | `parsers/java`, `java_springboot`, `java_jaxrs`, `java_vertx`, `kotlin`, `kotlin_ktor`, `scala`, `scala_play`, `scala_akkahttp`, `scala_http4s`, `groovy`, `groovy_vertx` |
| `14-symbols/parsers-dotnet.md` | `parsers/csharp`, `csharp_aspnet`, `csharp_graphql`, `csharp_hotchocolate`, `csharp_wcf`, `csharp_webforms`, `razor`, `vb`, `vb_aspnet`, `dotnet_servicehost` |
| `14-symbols/parsers-js-ts.md` | `parsers/typescript`, `typescript_react`, `typescript_angular`, `typescript_vue`, `typescript_nestjs`, `typescript_nextjs`, `typescript_express`, `typescript_fastify`, `typescript_graphql`, `typescript_graphql_codefirst`, `typescript_loopback`, `html` |
| `14-symbols/parsers-other.md` | `parsers/python`, `python_fastapi`, `ruby`, `ruby_rails`, `ruby_grape`, `ruby_sinatra`, `c`, `cfamily_common`, `cpp`, `graphql`, `prisma`, `hcl`, `hcl_terraform`, `structured_json`, `config` |
| `14-symbols/tests-and-tooling.md` | `tests/unit/`, `scripts/`, `skills/extend-capture/`, `.github/workflows/main.yml`, `pyproject.toml` |
