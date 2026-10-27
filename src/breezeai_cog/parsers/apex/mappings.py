"""Apex AST node-type classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "enhanced_for_statement",
    "while_statement",
    "do_statement",
    "switch_expression",
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
    "local_variable_declaration",
    "field_declaration",
    "expression_statement",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

#: Scopes extracted as their own Class/Function, so statement walks of the enclosing
#: scope do not descend into them.
NESTED_SCOPES = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "method_declaration",
    "constructor_declaration",
    "trigger_declaration",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)

COMMENT_TYPES = {"line_comment", "block_comment"}

FRAMEWORKS = ["apex-rest", "apex-aura", "apex-soap", "apex-trigger", "apex-platform-event",
              "apex-schedule"]

TYPE_DECLARATIONS = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
}
