"""Capability metadata for the standalone GraphQL SDL/operation parser.

``STATEMENT_TYPES`` are the real tree-sitter ``graphql`` node types this parser emits as
``Statement.nodeType`` — used for capability discovery (``breezeai-cog capabilities``).
Unlike the embedded ``gql`` path (which emits ``nodeType="synthetic"`` because the SDL lives
inside a TS template string with no host AST node), a standalone ``.graphql`` file has a real
GraphQL AST, so each record keeps its genuine grammar node type.
"""

from __future__ import annotations

#: GraphQL grammar node types emitted as Statement.nodeType (discovered empirically). The
#: type system (type/interface/enum/union/input) is emitted as Class nodes, not statements —
#: only ``object_type_definition`` appears here, as the nodeType of the ``graphql_entity``
#: statement carried by a ``@key`` type. Members and operations are the rest.
STATEMENT_TYPES: list[str] = [
    "object_type_definition",  # @key federation-entity marker statement
    "field_definition",  # object/interface field (member) + root-type route
    "input_value_definition",  # input-object field (member)
    "enum_value_definition",  # enum value (member)
    "named_type",  # union member
    "scalar_type_definition",
    "directive_definition",
    "schema_definition",
    "field",  # invoked field of a client operation (api_call)
    "operation_definition",
    "fragment_definition",
]

#: Frameworks this parser reports (single-purpose — the SDL/operation surface).
FRAMEWORKS: list[str] = ["graphql"]

#: Comment node types for the shared whole-file comment pass. ``comment`` = ``# …`` lines;
#: ``description`` = ``"""…"""`` SDL docstrings on types/fields/values (captured as ``comment``
#: statements like Python docstrings — deduped by the pass where already inside a member
#: statement's ``text``).
COMMENT_TYPES: frozenset[str] = frozenset({"comment", "description"})
