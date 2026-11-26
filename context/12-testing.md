---
title: "breezeai-cog — Testing"
date: 2026-10-30
---

Scope: Test layout, how to run, fixtures, coverage posture.

## Test framework

- `pytest ≥8` + `pytest-timeout ≥2.3`
- `jsonschema ≥4.21` (schema validation tests)
- `testpaths = ["tests"]`; `pythonpath = ["src"]` (set in `pyproject.toml`)
- Tests are under `tests/unit/`.

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

# Single test module
uv run pytest tests/unit/test_c_parser.py

# Single file
uv run pytest tests/unit/test_cli.py

# Single test by name
uv run pytest -k "test_java"

# With timeout override
uv run pytest --timeout=120
```

## Test posture

- **Test coverage**: tests cover server routes, SCM integrations, batch upload, and Groovy parsing. Avoid inferring that an area has no tests without checking the current test suite.
- **Parser tests**: each test provides a synthetic source snippet and asserts on the `FileRecord` fields returned by the parser.
- **External services**: the suite includes SCM integration tests; it does not exercise a live backend or real S3 service.
- **CI**: no automated test run on PR (CI only validates PR title). Tests are run manually by developers.

## Coverage notes

The test suite includes coverage for server routes, SCM integrations, batch upload, and Groovy
parsing. Consult the current tests before making claims about coverage gaps; this snapshot does
not maintain an exhaustive gap inventory.

## Fixtures

- Tests create `ParseContext` directly with in-memory `source` bytes.
- `pytest-timeout` guards against infinite tree-sitter parse loops (common during grammar development).
- `jsonschema` used in `test_schema.py` to validate emitted records against the generated JSON Schema.
