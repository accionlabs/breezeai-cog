"""Capability metadata for the standalone JSLT (JSON transform language) parser."""

from __future__ import annotations

#: Node types this parser emits as Statement.nodeType, from the hand-rolled line-scan (see
#: `.statements` for why there's no tree-sitter grammar backing this).
STATEMENT_TYPES: list[str] = ["import", "function_def", "let_binding", "module_expression"]

#: Frameworks this parser reports (single-purpose — the transform-script surface).
FRAMEWORKS: list[str] = ["jslt"]
