"""Go AST classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "expression_switch_statement",
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
    "fallthrough_statement",
}

# A bare call is always wrapped in ``expression_statement`` (or ``go``/``defer``), so
# ``call_expression`` is not listed: listing it would emit every call a second time and
# stop the shared classifier from seeing the call inside its statement.
DECLARATIONS = {
    "expression_statement",
    "assignment_statement",
    "short_var_declaration",
    "var_declaration",
    "const_declaration",
    "send_statement",
    "inc_statement",
    "dec_statement",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

# Scopes extracted as their own Function. A function literal is one only when it is stored in
# a variable (``var notify = func…``); an inline literal is walked, its statements belonging to
# the enclosing function, or to the file at package level.
NESTED_SCOPES = {
    "function_declaration",
    "method_declaration",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)
COMMENT_TYPES = {"line_comment", "block_comment"}
FRAMEWORKS: list[str] = []
