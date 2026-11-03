---
title: "breezeai-cog — Symbol Reference: Tests and Tooling"
date: 2026-10-30
---

Scope: `tests/unit/`, `scripts/`, `skills/extend-capture/`, `.github/workflows/`, `pyproject.toml`.

## tests/unit/

166 test files. All use `pytest`. Key groups:

| Path | Covers |
|---|---|
| `tests/unit/test_cli.py` | CLI command dispatch, flag parsing, batch mode |
| `tests/unit/test_config.py` | `Settings` validation, env aliases, upload guards |
| `tests/unit/test_pipeline.py` | `core/pipeline` scan, assemble, run |
| `tests/unit/test_schema.py` | Capture contract fields, JSON Schema generation, `SCHEMA_VERSION` match |
| `tests/unit/test_c_parser.py` | C parser: includes, struct/union/enum records, typedef `alias`, K&R definitions, `extern "C"` calls into C++ |
| `tests/unit/test_cpp_parser.py` | C++ parser: classes, namespaces, templates, cross-file call resolution |
| `tests/unit/test_ownership.py` | Ownership step: content, references, neighbours/repo and default rules; `cpp_only_syntax`; `--language` runs |
| `tests/unit/test_<lang>_parser.py` | One flat file per language/framework |
| `tests/unit/infra/`, `tests/unit/integrations/` | Storage sink and SCM provider tests |

## scripts/

Utility scripts (e.g. batch-upload helpers, one-off data processing). Not part of the production runtime. Content varies by baseline.

## skills/extend-capture/

A Claude Code skill that guides adding a new parser to cog. Used with `@extend-capture` in Claude Code sessions. Content: step-by-step parser addition recipe, template files, test scaffolding instructions.

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
