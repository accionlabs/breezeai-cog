"""Capability metadata for the standalone Prisma Schema Language (PSL) parser.

``STATEMENT_TYPES`` are the real tree-sitter ``prisma`` node types this parser emits as
``Statement.nodeType`` (discovered empirically against the language-pack ``prisma`` grammar,
ABI v13) — used for capability discovery (``breezeai-cog capabilities``). A standalone
``.prisma`` file has a real Prisma AST, so each record keeps its genuine grammar node type.

Empirical grammar note (v13): ``type X { … }`` composite-type blocks (MongoDB embedded
documents) and *empty* ``model``/``enum`` blocks parse to ``ERROR`` nodes, so this parser
captures neither — an honest gap, never a guess (see the parser's module docstring).
"""

from __future__ import annotations

#: Prisma grammar node types emitted as Statement.nodeType (discovered empirically).
STATEMENT_TYPES: list[str] = [
    "model_block",
    "enum_block",
    "key_value_block",
]

#: Frameworks this parser reports (single-purpose — the schema-definition surface).
FRAMEWORKS: list[str] = ["prisma"]

#: Comment node types for the shared whole-file comment pass. The Prisma grammar uses
#: ``comment`` for ``// …`` and ``document_comment`` for ``/// …`` (the field/model doc form).
COMMENT_TYPES: frozenset[str] = frozenset({"comment", "document_comment"})
