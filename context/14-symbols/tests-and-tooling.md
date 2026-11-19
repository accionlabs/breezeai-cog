---
title: "breezeai-cog — Symbol Reference: Tests and Tooling"
date: 2026-10-30
---

Scope: `tests/`, `scripts/`, `skills/`, `.github/workflows/`, `pyproject.toml`.

## tests/unit/

The suite uses `pytest`. Run it with `uv run pytest -q`; see `AGENTS.md` for setup and check
commands. Consult the current test files before making claims about coverage.

## scripts/

Utility scripts (including `bench_parse_scaling.py`). Not part of the production runtime.

## skills/

The `extend-capture` and `gap-review` skills are under `skills/`; each currently contains its `SKILL.md`. Use these maintained skills for parser work and review.

## .github/workflows/main.yml

Name: **PR Title Validation**. Only CI: enforces `BREEZEAI-<N>` in PR title. No build, test, or lint step.

## pyproject.toml

| Section | Notes |
|---|---|
| `[project.dependencies]` | Core: pydantic, typer, structlog, pathspec, rich, tree-sitter (pinned), httpx, toon-format |
| `[project.optional-dependencies]` | `server`: fastapi, uvicorn, gitpython, python-multipart, boto3, sqlglot |
| `[dependency-groups]` | `dev`: pytest, pytest-timeout, jsonschema, ruff, mypy |
| `[project.scripts]` | `breezeai-cog = "breezeai_cog.cli:main"` |
| `[tool.pytest.ini_options]` | `testpaths=["tests"]`, `pythonpath=["src"]` |
| `[tool.ruff]` | `line-length=100`, `target-version="py311"` |
| `[tool.mypy]` | `strict=true`, `python_version="3.11"` |
