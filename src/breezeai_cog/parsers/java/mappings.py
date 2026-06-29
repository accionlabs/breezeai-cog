"""Java AST node-type classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "enhanced_for_statement",
    "while_statement",
    "do_statement",
    "switch_expression",
    "try_statement",
    "try_with_resources_statement",
    "synchronized_statement",
}

JUMP = {
    "return_statement",
    "break_statement",
    "continue_statement",
    "throw_statement",
    "yield_statement",
}

DECLARATIONS = {
    "local_variable_declaration",
    "field_declaration",
    "expression_statement",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

NESTED_SCOPES = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "method_declaration",
    "constructor_declaration",
    "lambda_expression",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)

FRAMEWORKS = ["spring", "springboot", "jaxrs", "quarkus"]
