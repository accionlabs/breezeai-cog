---
title: "breezeai-cog — Context Index"
date: 2026-10-30
---

Scope: Master index and documentation router. Route repo questions not answered in AGENTS.md to the right context file.

Generated from commit `a6d5cf1` (branch `docs/cog-deep-context`, origin `development`).
To update: re-run `/deep-context-python` from the repo root.

## What is this repo

Python 3.11+ source-to-NDJSON code-ontology generator. Two interfaces: a **Typer CLI** (`breezeai-cog repo-to-json-tree`) and a **FastAPI HTTP service** (`breezeai-cog serve`). Parses a source repository into a gzipped NDJSON "capture contract" file that the `breezeai-backend` code-ontology endpoints ingest. Supports ~73 languages/frameworks via tree-sitter. Schema version: **2.4**.

## Routing table

| File | What it holds | Question / When to read |
|---|---|---|
| `context/01-overview.md` | Purpose, runtime stack, external dependencies, request flow diagram | Stack, versions, system architecture, service role |
| `context/02-setup-and-run.md` | Installation, uv commands, entrypoints, local ports, dev workflows | Setup, uv commands, ports, Docker, boot sequence |
| `context/03-configuration.md` | Complete inventory of environment variables and Settings fields | Env vars, settings fields, secrets, S3/SCM config |
| `context/04-architecture-and-conventions.md` | Layering, conventions, router mechanics, parser recipes | Layering, conventions, how to add a parser |
| `context/05-data-model.md` | NDJSON capture contract, Pydantic models, JSON Schema generation | Capture contract, schema, record types, SCHEMA_VERSION |
| `context/06-domain-core-model.md` | Parse pipeline, language detection, AST traversal, parser anatomy | Parse pipeline, parser anatomy, tree-sitter, detection |
| `context/07-api-surface.md` | CLI command options and FastAPI HTTP route definitions | CLI commands (`breezeai-cog ...`), HTTP routes |
| `context/08-services-and-business-logic.md` | AnalysisService, upload service, batch processing, diffs | Services: analysis, upload, batch, diff, analyzers |
| `context/09-integrations-and-async.md` | Backend upload, SCM clients, S3 infra, Node 22 requirement | Backend upload, SCM integrations, SCM REST API |
| `context/10-cross-cutting.md` | structlog logging, worker isolation, skips, error handling | Logging, error handling, concurrency, cross-cutting |
| `context/11-operations.md` | Docker multi-stage build, CI PR title validation, k8s deploy | Dockerfile, CI, k8s deploy yaml, runbook |
| `context/12-testing.md` | Test suite layout, running pytest, fixtures, coverage posture | Tests: layout, how to run, fixtures, coverage gaps |
| `context/13-glossary-and-open-questions.md` | Domain glossary, naming conventions, known foot-guns | Glossary, open questions, UNVERIFIED items |
| `context/14-symbol-reference.md` | Router to 8 per-subsystem symbol reference shards | What does X do? Where is symbol X defined? |
| `context/15-diagnosis-playbook.md` | Router + global symptom-to-suspect table and 3 diagnosis shards | Bug / error / why is X failing, how to fix |

Files 14 and 15 are **sharded**. Read the router file, then **only the one shard** that covers the question.

## Headline counts (verified from source)

| Item | Count |
|---|---|
| Source files (`src/`) | 453 |
| Test files (`tests/`) | 169 |
| Source bytes (`src/`) | ~3.0 MB |
| Parser packages (language/framework) | 75 |
| HTTP routes (server) | 13 |
| CLI commands | 5 |
| Env vars / settings fields | ~35 |
| Schema version | 2.4 |

## Coverage checklist

| Source path | Documenting file(s) |
|---|---|
| `src/breezeai_cog/cli.py` | 07-api-surface, 08-services |
| `src/breezeai_cog/config.py` | 03-configuration |
| `src/breezeai_cog/schemas/` | 05-data-model, 14-symbols/core-and-schemas |
| `src/breezeai_cog/core/` | 06-domain-core-model, 14-symbols/core-and-schemas |
| `src/breezeai_cog/parsers/` | 06-domain-core-model, 14-symbols/parser-* |
| `src/breezeai_cog/parsers/c/`, `parsers/cfamily_common/`, `parsers/cpp/` | 14-symbols/parsers-other, 15-diagnosis/parsers |
| `src/breezeai_cog/server/` | 07-api-surface, 14-symbols/services-server-integrations |
| `src/breezeai_cog/services/` | 08-services-and-business-logic, 14-symbols/services-server-integrations |
| `src/breezeai_cog/integrations/` | 09-integrations-and-async, 14-symbols/services-server-integrations |
| `src/breezeai_cog/analyzers/` | 08-services-and-business-logic |
| `src/breezeai_cog/emit/` | 06-domain-core-model |
| `src/breezeai_cog/infra/` | 09-integrations-and-async |
| `src/breezeai_cog/utils/` | 10-cross-cutting |
| `src/breezeai_cog/logging.py` | 10-cross-cutting |
| `tests/unit/` | 12-testing |
| `docs/`, `skills/` | 12-testing, 04-architecture-and-conventions |
| `.github/workflows/main.yml` | 11-operations |
| `Dockerfile`, `breezeai-cog-deploy.yaml` | 11-operations |
| `pyproject.toml` | 02-setup-and-run |
