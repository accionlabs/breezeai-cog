"""VB.NET AST node-type classification + capability metadata.

VB wraps every in-body statement in a ``statement`` node (unwrapped one level during
extraction), so the sets below name the *inner* node types."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "for_each_statement",
    "while_statement",
    "do_loop_statement",
    "do_statement",
    "select_statement",
    "select_case_statement",
    "try_statement",
    "using_statement",
    "with_statement",
    "synclock_statement",
}

JUMP = {
    "return_statement",
    "exit_statement",
    "continue_statement",
    "throw_statement",
}

DECLARATIONS = {
    "dim_statement",
    "call_statement",
    "field_declaration",
    "local_declaration_statement",
    "assignment_statement",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

NESTED_SCOPES = {
    "class_block",
    "interface_block",
    "enum_block",
    "struct_block",
    "structure_block",
    "module_block",
    "method_declaration",
    "constructor_declaration",
    "lambda_expression",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)

FRAMEWORKS = ["aspnet", "aspnetcore"]
