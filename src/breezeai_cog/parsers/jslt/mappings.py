"""Capability metadata for the standalone JSLT (JSON transform language) parser."""

from __future__ import annotations

#: Node types this parser emits as Statement.nodeType. JSLT has no tree-sitter grammar (see
#: `.statements`), so no record has a backing AST node: every statement — `import`, `let`,
#: the trailing output expression and each `def`'s body — is `synthetic`, and which one it is
#: is read from its `text`.
STATEMENT_TYPES: list[str] = ["synthetic"]

#: Frameworks this parser reports: none — JSLT is a language, with no framework on top.
FRAMEWORKS: list[str] = []
