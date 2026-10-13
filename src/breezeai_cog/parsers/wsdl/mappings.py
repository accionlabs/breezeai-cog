"""Capability metadata for the standalone WSDL 1.1 (SOAP service contract) parser."""

from __future__ import annotations

from ..xsd.mappings import STATEMENT_TYPES as _XSD_STATEMENT_TYPES

#: Local WSDL tag names, plus every tag ``xsd/schema.py`` can emit when reused for the
#: embedded ``<wsdl:types><xsd:schema>…`` — kept as one source of truth (xsd/mappings.py) so
#: this list can't silently drift out of sync with what that walker actually emits.
STATEMENT_TYPES: list[str] = ["message", "operation", *_XSD_STATEMENT_TYPES]

#: Frameworks this parser reports (single-purpose — the SOAP service-contract surface).
FRAMEWORKS: list[str] = ["wsdl"]

#: The ``xml`` tree-sitter grammar's comment node type (verified empirically), for
#: ``<!-- … -->``.
COMMENT_TYPES: frozenset[str] = frozenset({"Comment"})
