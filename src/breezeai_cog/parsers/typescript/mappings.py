"""TypeScript/JavaScript AST node-type classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "try_statement",
}

JUMP = {
    "return_statement",
    "break_statement",
    "continue_statement",
    "throw_statement",
}

DECLARATIONS = {
    "lexical_declaration",
    "variable_declaration",
    "expression_statement",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

#: Scopes whose inner statements belong to that nested scope.
NESTED_SCOPES = {
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "class_declaration",
    "class",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)

FRAMEWORKS = ["angular", "nestjs", "loopback", "express", "react", "vue"]
