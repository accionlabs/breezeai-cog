---
title: "breezeai-cog — Architecture and Conventions"
date: 2026-10-30
---

Scope: Routing to maintained architecture documentation; this file is not a second architecture or parser reference.

## Architecture

For the capture contract, statement model, cross-file resolution, graph mapping, and parser
architecture, use [`docs/architecture.md`](../docs/architecture.md) and its focused documents
under [`docs/architecture/`](../docs/architecture/). Those documents and the source define the
current behavior.

## Parser and detector work

Use [`skills/extend-capture/SKILL.md`](../skills/extend-capture/SKILL.md) for changes and
[`skills/gap-review/SKILL.md`](../skills/gap-review/SKILL.md) with
[`docs/parser-review-guide.md`](../docs/parser-review-guide.md) for reviews and gap analysis.
The maintained [`docs/parser-reference.md`](../docs/parser-reference.md) describes parser
building blocks and conventions. Do not use this context set as a parser implementation recipe.
