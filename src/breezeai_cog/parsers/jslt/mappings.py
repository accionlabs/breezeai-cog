"""Capability metadata for the standalone JSLT (JSON transform language) parser."""

from __future__ import annotations

#: Node types this parser emits as Statement.nodeType, from the hand-rolled line-scan (see
#: `.statements` for why there's no tree-sitter grammar backing this). `function_body` is a
#: `def`'s full-text companion statement (Function has no `text` field), parented to the
#: Function it belongs to, not to the file.
STATEMENT_TYPES: list[str] = ["import", "let_binding", "module_expression", "function_body"]

#: Frameworks this parser reports (single-purpose — the transform-script surface).
FRAMEWORKS: list[str] = ["jslt"]
