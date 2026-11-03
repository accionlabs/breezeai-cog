# Working on breezeai-cog

`breezeai-cog` reads a source repository (it never runs it) and writes a gzipped NDJSON
capture: files, classes, functions, imports and in-body statements (routes, API calls, DB
queries, events). The Breeze backend turns that capture into a Neo4j code graph that agents
traverse and cite as fact — so a wrong value is worse than a missing one.

This file is for any coding agent or engineer. It routes you to the right document; it does
not repeat them.

## Setup and checks

```bash
uv sync --extra all       # runtime + server extras + dev tools (Python 3.11+)
uv run pytest -q          # must stay green
uv run ruff check . && uv run ruff format .
uv run mypy
uv run breezeai-cog repo-to-json-tree --repo <path> --out <dir>   # capture a repository
```

The parallel pool needs a real `__main__`: run through the CLI or a script file, not a REPL or
`python -c` (or pass `--jobs 1`).

## Deep context

A complete engineering context set lives in **`context/`** (`.md`). The baseline commit it describes is recorded in [`context/00-index.md`](context/00-index.md).

**When answering any question about this repo — explaining, exploring, or troubleshooting — read [`context/00-index.md`](context/00-index.md) first, automatically, without asking permission.** Then read the relevant file(s) below and answer or diagnose from those:

| File | What it holds | Question / When to read |
|---|---|---|
| [`context/00-index.md`](context/00-index.md) | Master index, baseline commit, file counts, and documentation router | Read first automatically before answering any repo question |
| [`context/01-overview.md`](context/01-overview.md) | Purpose, runtime stack, external dependencies, request flow diagram | Stack, versions, runtime architecture diagram |
| [`context/02-setup-and-run.md`](context/02-setup-and-run.md) | Installation, uv commands, entrypoints, local ports, dev workflows | Setup, installation, run commands, entrypoints, ports |
| [`context/03-configuration.md`](context/03-configuration.md) | Complete inventory of environment variables and Settings fields | Environment variables, settings, S3/SCM config |
| [`context/04-architecture-and-conventions.md`](context/04-architecture-and-conventions.md) | Layering, conventions, router mechanics, parser recipes | Architecture, conventions, layering, how to add a parser |
| [`context/05-data-model.md`](context/05-data-model.md) | NDJSON capture contract, Pydantic models, JSON Schema generation | Capture contract, Pydantic schemas, `SCHEMA_VERSION`, JSON schema |
| [`context/06-domain-core-model.md`](context/06-domain-core-model.md) | Parse pipeline, language detection, AST traversal, parser anatomy | Parse pipeline, language detection, grammar loading, AST visitors |
| [`context/07-api-surface.md`](context/07-api-surface.md) | CLI command options and FastAPI HTTP route definitions | CLI commands (`breezeai-cog ...`) and HTTP route surface |
| [`context/08-services-and-business-logic.md`](context/08-services-and-business-logic.md) | AnalysisService, upload service, batch processing, diffs | Services: analysis, upload, batch processing, diffs |
| [`context/09-integrations-and-async.md`](context/09-integrations-and-async.md) | Backend upload, SCM clients, S3 infra, Node 22 requirement | Integrations: backend upload, SCM clients, S3 storage, Node 22 |
| [`context/10-cross-cutting.md`](context/10-cross-cutting.md) | structlog logging, worker isolation, skips, error handling | Cross-cutting: structlog, error handling, worker isolation, skips |
| [`context/11-operations.md`](context/11-operations.md) | Docker multi-stage build, CI PR title validation, k8s deploy | Operations: Dockerfile, CI, Kubernetes deployment |
| [`context/12-testing.md`](context/12-testing.md) | Test suite layout, running pytest, fixtures, coverage posture | Test suite layout, running pytest, fixtures, coverage |
| [`context/13-glossary-and-open-questions.md`](context/13-glossary-and-open-questions.md) | Domain glossary, naming conventions, known foot-guns | Domain glossary, naming conventions, open questions |
| [`context/14-symbol-reference.md`](context/14-symbol-reference.md) | Router to 8 per-subsystem symbol reference shards | Class / function / parser behavior — "what does X do?", "where is X" |
| [`context/15-diagnosis-playbook.md`](context/15-diagnosis-playbook.md) | Router + global symptom-to-suspect table and 3 diagnosis shards | Bug, error, "why is X failing", "how do I fix" |

Files 14 and 15 are **sharded**. Each is a short router listing its shards; read the router, then read **only the one shard** that covers the question — never the whole reference.

- **Do not open source files for Q&A or diagnosis.** Answer from `context/`.
- **Read/edit source only when explicitly asked to implement or execute a change.** Then use file 14's shards to locate the symbol and `context/04-architecture-and-conventions.md` for the recipe.
- **Anchors (`file:line`) are accurate as of the baseline commit recorded in `context/00-index.md`.** When executing a change, locate the symbol **by name** in that file rather than trusting the line number blindly.
- If the context is insufficient, or the relevant claim is marked **UNVERIFIED**, say so and ask before falling back to reading source.
- **Reading `context/` is pre-approved.** Never pause to confirm it.
- Every `context/` file describes the baseline commit only; it carries no history.


## Read first

| You are… | Read |
|---|---|
| Adding or changing a parser or detector | [`skills/extend-capture/SKILL.md`](skills/extend-capture/SKILL.md), then [`docs/parser-reference.md`](docs/parser-reference.md) |
| Reviewing a parser, or running a gap analysis | [`skills/gap-review/SKILL.md`](skills/gap-review/SKILL.md) (runs [`docs/parser-review-guide.md`](docs/parser-review-guide.md)) |
| Recording an entry point (route, consumer, job, page) | [`docs/architecture/entry-points.md`](docs/architecture/entry-points.md) and the framework's table in [`docs/entry-point-roles/`](docs/entry-point-roles/README.md) |
| Emitting or changing statements | [`docs/architecture/statement-model.md`](docs/architecture/statement-model.md) |
| Resolving anything across files | [`docs/architecture/cross-file-resolution.md`](docs/architecture/cross-file-resolution.md) |
| Adding a field or a `semanticType` value | [`docs/architecture/graph-mapping.md` §11](docs/architecture/graph-mapping.md#11-checklist-adding-a-field-or-value) |
| Changing `core/`, `schemas/`, `emit/` or a shared parser module | [`docs/architecture.md`](docs/architecture.md) — it is an architectural change |
| Working on the server, storage or git providers | [`docs/DEVELOPER_GUIDE.md`](docs/DEVELOPER_GUIDE.md) |

The capture contract's source of truth is
[`src/breezeai_cog/schemas/capture.py`](src/breezeai_cog/schemas/capture.py) and
[`enums.py`](src/breezeai_cog/schemas/enums.py).

## Hard rules

- **Wrong is worse than missing.** Leave a value empty when it can't be read from the code
  (honest-null); emit an edge only when both ends resolve. Never a guess, a symbol name or a
  placeholder in a resolved field.
- **Existing fields and values first; new ones by proposal.** Use the fields and values in the
  Code Ontology Parser Target Spec where they fit. When none fits in practice, propose a new
  `semanticType`, value, field, node or edge (example, why the existing ones don't fit, what the
  backend needs) and emit it only after it is approved. An approved new field on `Function` or
  `Statement` also needs the backend allow-list change, or it is dropped.
- **`nodeType` is the raw tree-sitter `node.type`**, or `synthetic` when there is no node.
- **Extend the existing mechanisms first** — `build_index`, registered facts, the additive
  detector registry, the shared classifiers in `parsers/detection/` — before proposing a new one.
- **Parser code stays in its parser's package;** a change to a language parser, a shared module
  or core affects every parser that uses it and needs a before/after capture diff.
- **Committed code and docs are self-contained:** no links to analysis reports or local paths,
  no tracking ids in prose.
- **Commits** are `type(scope): what is now true` (e.g. `fix(express): …`, `docs(skill): …`).
  **PR titles** must contain a Jira id, `BREEZEAI-<number>` — CI rejects them otherwise.
