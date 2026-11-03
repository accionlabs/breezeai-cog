"""Capability metadata for the standalone WSDL 1.1 (SOAP service contract) parser."""

from __future__ import annotations

from ..xsd.mappings import STATEMENT_TYPES as _XSD_STATEMENT_TYPES

#: Tree-sitter node types emitted as Statement.nodeType. WSDL is parsed with the same ``xml``
#: grammar as XSD (whose walker it reuses for embedded ``<wsdl:types>`` schemas), so the list
#: is XSD's — one source of truth.
STATEMENT_TYPES: list[str] = list(_XSD_STATEMENT_TYPES)

#: Frameworks this parser reports (single-purpose — the SOAP service-contract surface).
FRAMEWORKS: list[str] = ["wsdl"]

#: The ``xml`` tree-sitter grammar's comment node type (verified empirically), for
#: ``<!-- … -->``.
COMMENT_TYPES: frozenset[str] = frozenset({"Comment"})
