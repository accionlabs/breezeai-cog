"""Mappings and AST node types for the PHP parser."""

from __future__ import annotations

#: Structural statement node types emitted when --capture-statements is on.
EMIT_TYPES: frozenset[str] = frozenset(
    {
        "expression_statement",
        "if_statement",
        "while_statement",
        "do_statement",
        "for_statement",
        "foreach_statement",
        "switch_statement",
        "try_statement",
        "return_statement",
        "throw_statement",
        "echo_statement",
        "global_declaration",
        "goto_statement",
        "break_statement",
        "continue_statement",
        "declare_statement",
        "unset_statement",
    }
)

#: Control-flow statements whose text is kept only as the header line.
CONTROL_FLOW: frozenset[str] = frozenset(
    {
        "if_statement",
        "while_statement",
        "do_statement",
        "for_statement",
        "foreach_statement",
        "switch_statement",
        "try_statement",
    }
)

#: AST node types that are barriers for non-descending statement walks.
NESTED_SCOPES: frozenset[str] = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "trait_declaration",
        "enum_declaration",
        "function_definition",
        "method_declaration",
    }
)

#: Tree-sitter comment node types.
COMMENT_TYPES: tuple[str, ...] = ("comment",)

#: Statement node types for capability discovery.
STATEMENT_TYPES: list[str] = sorted(
    EMIT_TYPES
    | {
        "class_declaration",
        "interface_declaration",
        "trait_declaration",
        "enum_declaration",
        "function_definition",
        "method_declaration",
        "comment",
        "enum_case",
    }
)

FRAMEWORKS: list[str] = []
