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
- **Ask before new vocabulary.** A new `semanticType`, field, node type or edge, or any value
  not in the Code Ontology Parser Target Spec, needs a human decision before it is coded. A new
  field on `Function` or `Statement` is dropped by the backend unless its allow-list changes too.
- **`nodeType` is the raw tree-sitter `node.type`**, or `synthetic` when there is no node.
- **Extend the existing mechanisms first** — `build_index`, registered facts, the additive
  detector registry, the shared classifiers in `parsers/detection/` — before proposing a new one.
- **Parser code stays in its parser's package;** a change to a language parser, a shared module
  or core affects every parser that uses it and needs a before/after capture diff.
- **Committed code and docs are self-contained:** no links to analysis reports or local paths,
  no tracking ids in prose.
- **Commits** are `type(scope): what is now true` (e.g. `fix(express): …`, `docs(skill): …`).
  **PR titles** must contain a Jira id, `BREEZEAI-<number>` — CI rejects them otherwise.
