"""Go AST classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "range_statement",
    "switch_statement",
    "type_switch_statement",
    "select_statement",
    "go_statement",
    "defer_statement",
}

JUMP = {
    "return_statement",
    "break_statement",
    "continue_statement",
    "goto_statement",
}

DECLARATIONS = {
    "expression_statement",
    "assignment_statement",
    "short_var_declaration",
    "var_declaration",
    "const_declaration",
    "call_expression",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

NESTED_SCOPES = {
    "function_declaration",
    "method_declaration",
    "func_literal",
    "block",
    "if_statement",
    "for_statement",
    "range_statement",
    "switch_statement",
    "type_switch_statement",
    "select_statement",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)
COMMENT_TYPES = {"line_comment", "block_comment"}
FRAMEWORKS = ["net_http", "gin", "echo", "fiber", "chi", "gorilla_mux", "grpc"]
