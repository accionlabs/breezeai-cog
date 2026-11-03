"""PHP AST node mappings and capability metadata."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "else_if_clause",
    "else_clause",
    "for_statement",
    "foreach_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "try_statement",
    "catch_clause",
    "finally_clause",
}

JUMP = {
    "return_statement",
    "break_statement",
    "continue_statement",
    "throw_statement",
}

DECLARATIONS = {
    "expression_statement",
    "property_declaration",
    "const_declaration",
    "case_statement",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

NESTED_SCOPES = {
    "class_declaration",
    "interface_declaration",
    "trait_declaration",
    "enum_declaration",
    "method_declaration",
    "function_definition",
    "anonymous_function",
    "arrow_function",
}

COMMENT_TYPES = {"comment"}
STATEMENT_TYPES = sorted(EMIT_TYPES)
FRAMEWORKS = ["laravel", "codeigniter", "slim", "symfony", "wordpress"]
