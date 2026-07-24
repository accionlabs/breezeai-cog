"""Closed vocabularies from the capture coverage schema (source of truth).

Mirrors the ``enum`` constraints in ``code-capture-coverage-schema.json``. Kept as
``Literal`` aliases so they validate in Pydantic and round-trip into JSON Schema.
"""

from __future__ import annotations

from typing import Literal

# fileRecord.type
FileType = Literal["code", "config"]

# class.type
ClassType = Literal[
    "class",
    "interface",
    "struct",
    "record",
    "enum",
    "module",
]

# statement.semanticType — the schema's enum also allows null, expressed in the
# model as ``SemanticType | None``.
SemanticType = Literal[
    "route",
    "api_call",
    "db_method_call",
    "query_statement",
    "eventbus_send",
    "eventbus_publish",
    "eventbus_consumer",
    "verticle_deploy",
    "service_proxy",
    "timer",
    "graphql_entity",
]
