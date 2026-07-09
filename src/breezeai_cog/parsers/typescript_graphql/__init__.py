"""GraphQL parser. Exposes ``PARSERS`` for ``discover_builtin``; selected per-file over
the base TypeScript parser via ``claims`` (a resolver map or GraphQL SDL)."""

from __future__ import annotations

from .parser import GraphQLParser

PARSERS = [GraphQLParser()]
