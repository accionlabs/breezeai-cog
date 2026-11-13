---
title: "breezeai-cog — Setup and Run"
date: 2026-10-30
---

Scope: Prerequisites, uv commands, entrypoints, ports, Docker, local-dev gotchas.

## Prerequisites

- Python ≥3.11
- `uv` (PEP 517 package manager)
- Node 22 on the host when driven by the `breeze:onboard-repository` skill (Node 24 fails silently on tree-sitter native bindings)

## Install and sync

```bash
# Standard dev install (includes dev group: pytest, ruff, mypy)
uv sync

# With FastAPI server extras (fastapi, uvicorn, gitpython, boto3, sqlglot)
uv sync --extra server

# All extras
uv sync --extra all
```

## CLI entrypoint

Installed script: `breezeai-cog` → `breezeai_cog.cli:main`

```bash
# Analyze a local repo and write .ndjson.gz to <repo>/.cog/
uv run breezeai-cog repo-to-json-tree --repo /path/to/repo

# Analyze and upload to backend
uv run breezeai-cog repo-to-json-tree --repo /path/to/repo --upload \
  --baseurl http://localhost:3000 --uuid <project-uuid> --user-api-key <key>

# Batch: analyze every subdirectory of a workspace folder
uv run breezeai-cog repo-to-json-tree --repo /workspace --batch

# Print supported languages/frameworks as JSON (derived from the parser registry)
uv run breezeai-cog capabilities

# Generate the capture JSON Schema (derived from Pydantic models)
uv run breezeai-cog schema

# Print tool version
uv run breezeai-cog version

# Start FastAPI service (default port 3000)
uv run breezeai-cog serve [--port 8080] [--host 0.0.0.0]
```

## Server boot chain (file by file)

1. `cli.py:serve()` — creates `Settings`, calls `uvicorn.run(create_app(settings), ...)`
2. `server/app.py:create_app()` — builds `FastAPI` app, attaches `settings` and `deps` to `app.state`, registers exception handlers, includes the single `router`
3. `server/deps.py:default_deps()` — constructs `ServerDeps` (the infra adapter: S3 storage + backend notify HTTP client)
4. `server/routes.py` — defines all HTTP routes; includes `git_router` from `server/git_routes.py`

No lifespan startup hook; no Neo4j or DB connection. State is per-request.

## Ports

| Context | Port | Setting |
|---|---|---|
| Local dev (CLI `serve`) | 3000 | `BREEZEAI_COG_PORT` / `--port` / `$PORT` |
| Docker / k8s | 3004 | set in `breezeai-cog-deploy.yaml` |

## Docker

```bash
docker build -t breezeai-cog .
docker run -p 3000:3000 breezeai-cog
```

The `Dockerfile` uses a multi-stage build; installs with `uv sync --extra server --no-dev`.

## Ruff / mypy / pre-commit

```bash
uv run ruff check src tests          # lint
uv run ruff format src tests         # format
uv run mypy                          # type-check (strict; packages = breezeai_cog)
```

`ruff` line-length: 100. `mypy` strict mode, Python 3.11 target.

## Tests

```bash
uv run pytest                        # all tests (testpaths = tests)
uv run pytest tests/unit/parsers/    # single subdirectory
uv run pytest tests/unit/test_cli.py # single file
uv run pytest -k "test_java"         # single test by name
uv run pytest --timeout=60           # override timeout (pytest-timeout installed)
```

## Windows / event-loop note

The executor uses `mp.get_context("spawn")` explicitly — cross-platform safe. No `asyncio` event-loop policy changes are needed. The CLI is synchronous at the top level; the server uses FastAPI's standard uvicorn loop.
