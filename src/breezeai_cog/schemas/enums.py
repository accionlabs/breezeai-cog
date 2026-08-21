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
    "union",
    "record",
    "enum",
    "module",
    "trait",
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
    # data/metadata document (not a behaviour) — the whole content of a captured
    # structured-JSON file, serialized as TOON on the statement `text`. Format-independent
    # by design (see structured_json parser); the serialization is carried on `framework`.
    # The backend persists semanticType verbatim (no enum check) and indexes it, so this
    # value ingests + filters with no backend change; adding it to the Confluence spec's
    # semanticType list (§2.4) is a documentation step, not a functional prerequisite.
    "structured_data",
    # source comment (not a behaviour) — a comment string captured as a first-class
    # Statement (keeping its real structural ``nodeType``: ``comment`` / ``line_comment`` /
    # ``block_comment`` / ``string`` for a Python docstring …) so it is embedded, scoped and
    # searchable. Emitted by the shared comment pass (``parsers/comments_common``) for every
    # language, plus Python docstrings. Persisted verbatim by the backend like
    # ``structured_data`` above.
    "comment",
    # IaC block / attribute types (HCL/Terraform family)
    "iac_resource",
    "iac_data_source",
    "iac_module",
    "iac_provider",
    "iac_variable",
    "iac_output",
    "iac_local",
    "iac_settings",
    "iac_variable_value",
]
