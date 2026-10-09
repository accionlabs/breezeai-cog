"""Capability metadata for the standalone XML Schema (XSD) parser.

``STATEMENT_TYPES`` are the local (prefix-stripped) XSD tag names this parser emits as
``Statement.nodeType`` — used for capability discovery (``breezeai-cog capabilities``).
"""

from __future__ import annotations

#: Local XSD tag names emitted as Statement.nodeType (global/top-level declarations only).
STATEMENT_TYPES: list[str] = [
    "element",
    "complexType",
    "simpleType",
    "group",
    "attributeGroup",
    "import",
    "include",
]

#: Frameworks this parser reports (single-purpose — the schema-definition surface).
FRAMEWORKS: list[str] = ["xsd"]

#: The ``xml`` tree-sitter grammar's comment node type (verified empirically), for
#: ``<!-- … -->``.
COMMENT_TYPES: frozenset[str] = frozenset({"Comment"})
