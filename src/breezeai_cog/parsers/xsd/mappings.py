"""Capability metadata for the standalone XML Schema (XSD) parser.

``STATEMENT_TYPES`` are the real tree-sitter ``xml`` node types this parser emits as
``Statement.nodeType`` — used for capability discovery (``breezeai-cog capabilities``). The
``xml`` grammar gives every tag the one node type ``element``, so that is the ``nodeType`` of
every XSD declaration; which declaration it is (``complexType``, ``group``, ``include``, …) is
read from its ``text``, never written into ``nodeType``.
"""

from __future__ import annotations

#: Tree-sitter node types emitted as Statement.nodeType (comments are added by the shared
#: comment pass with their own grammar type, ``COMMENT_TYPES``).
STATEMENT_TYPES: list[str] = ["element"]

#: Frameworks this parser reports (single-purpose — the schema-definition surface).
FRAMEWORKS: list[str] = ["xsd"]

#: The ``xml`` tree-sitter grammar's comment node type (verified empirically), for
#: ``<!-- … -->``.
COMMENT_TYPES: frozenset[str] = frozenset({"Comment"})
