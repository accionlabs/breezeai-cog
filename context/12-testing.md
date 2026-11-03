---
title: "breezeai-cog — Testing"
date: 2026-10-30
---

Scope: Test layout, how to run, fixtures, coverage posture.

## Test framework

- `pytest ≥8` + `pytest-timeout ≥2.3`
- `jsonschema ≥4.21` (schema validation tests)
- `testpaths = ["tests"]`; `pythonpath = ["src"]` (set in `pyproject.toml`)
- 166 test files, all under `tests/unit/`

## Layout

```
tests/
└── unit/
    ├── test_cli.py              CLI command tests
    ├── test_config.py           Settings validation tests
    ├── test_pipeline.py         Core pipeline tests
    ├── test_schema.py           Capture contract / JSON Schema tests
    ├── test_<lang>_parser.py    Per-parser tests, flat (majority of the suite)
    ├── test_c_parser.py         C parser: includes, structs/unions/enums, typedef alias, cross-file calls
    ├── test_cpp_parser.py       C++ parser
    ├── test_ownership.py        Ownership step: language of shared .h files, cpp_only_syntax
    ├── infra/                   Storage sink tests
    ├── integrations/            SCM provider tests
```

## How to run

```bash
# All tests
uv run pytest

# Single directory
uv run pytest tests/unit/test_c_parser.py

# Single file
uv run pytest tests/unit/test_cli.py

# Single test by name
uv run pytest -k "test_java_class"

# With timeout override
uv run pytest --timeout=120
```

## Test posture

- **Real cog**: ~166 test files is a large suite for a parser-heavy repo — this is the only cog/mcp/backend repo with meaningful test coverage.
- **Parser tests**: each test provides a synthetic source snippet and asserts on the `FileRecord` fields returned by the parser.
- **No integration tests**: no live backend, no real git repos, no S3 calls in the test suite.
- **CI**: no automated test run on PR (CI only validates PR title). Tests are run manually by developers.

## Coverage gaps

- Server routes: no automated tests for `/api/analyze-diff`, `/api/analyze-sql`, `/api/analyze-nosql`, `/api/analyze-es`.
- SCM provider integrations: no tests for the `integrations/scm/` modules (would require live provider credentials).
- Batch upload / resume: no tests for `services/batch_upload.py` resumable state.
- Groovy parser: limited test coverage due to the skeleton-only implementation.

## Fixtures

- Tests create `ParseContext` directly with in-memory `source` bytes.
- `pytest-timeout` guards against infinite tree-sitter parse loops (common during grammar development).
- `jsonschema` used in `test_schema.py` to validate emitted records against the generated JSON Schema.
