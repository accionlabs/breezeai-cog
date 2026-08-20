"""Capability metadata for the standalone GraphQL SDL/operation parser.

``STATEMENT_TYPES`` are the real tree-sitter ``graphql`` node types this parser emits as
``Statement.nodeType`` — used for capability discovery (``breezeai-cog capabilities``).
Unlike the embedded ``gql`` path (which emits ``nodeType="synthetic"`` because the SDL lives
inside a TS template string with no host AST node), a standalone ``.graphql`` file has a real
GraphQL AST, so each record keeps its genuine grammar node type.
"""

from __future__ import annotations

#: GraphQL grammar node types emitted as Statement.nodeType (discovered empirically).
STATEMENT_TYPES: list[str] = [
    "object_type_definition",
    "object_type_extension",
    "interface_type_definition",
    "union_type_definition",
    "enum_type_definition",
    "input_object_type_definition",
    "scalar_type_definition",
    "directive_definition",
    "schema_definition",
    "field_definition",
    "field",
    "operation_definition",
    "fragment_definition",
]

#: Frameworks this parser reports (single-purpose — the SDL/operation surface).
FRAMEWORKS: list[str] = ["graphql"]

#: Comment node types for the shared whole-file comment pass. The GraphQL grammar uses a
#: single ``comment`` node for ``# …``; ``"""…"""`` descriptions live inside their construct's
#: span and are absorbed into that construct's ``text`` (so they need no separate capture).
COMMENT_TYPES: frozenset[str] = frozenset({"comment"})
