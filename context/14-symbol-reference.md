---
title: "breezeai-cog — Symbol Reference (Router)"
date: 2026-10-30
---

Scope: Router for non-parser symbols. Read the router, then only the one shard that covers the question.

The capture contract is defined by `src/breezeai_cog/schemas/capture.py` and
`src/breezeai_cog/schemas/enums.py`; architecture documentation under `docs/architecture/`
is authoritative for the model and its use.

| Shard | Modules covered |
|---|---|
| `14-symbols/core-and-schemas.md` | `core/` pipeline, scanning, ownership, ignores and skips; `schemas/`; `emit/split` |
| `14-symbols/services-server-integrations.md` | `services/`, `server/`, `infra/`, `analyzers/`, `integrations/scm/` |
| `14-symbols/tests-and-tooling.md` | `tests/`, `scripts/`, `skills/`, `.github/workflows/main.yml`, `pyproject.toml` |

For parser and detector symbols, search the source by name and follow the parser references in
`AGENTS.md`; these shards are intentionally not maintained as a parser reference.
