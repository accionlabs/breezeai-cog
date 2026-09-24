"""Capability metadata for the standalone WSDL 1.1 (SOAP service contract) parser."""

from __future__ import annotations

#: Local WSDL tag names, plus the embedded-schema tag names reused from ``xsd`` for
#: ``<wsdl:types><xsd:schema>…``, emitted as Statement.nodeType.
STATEMENT_TYPES: list[str] = [
    "message",
    "import",
    "synthetic",  # merged portType/operation + binding/operation -> route
    "element",
    "complexType",
    "simpleType",
    "group",
    "attributeGroup",
]

#: Frameworks this parser reports (single-purpose — the SOAP service-contract surface).
FRAMEWORKS: list[str] = ["wsdl"]

#: The ``xml`` tree-sitter grammar's comment node type (verified empirically), for
#: ``<!-- … -->``.
COMMENT_TYPES: frozenset[str] = frozenset({"Comment"})
