"""C# AST node-type classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "for_each_statement",
    "foreach_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "switch_expression",
    "try_statement",
    "using_statement",
    "lock_statement",
    "checked_statement",
    "fixed_statement",
}

JUMP = {
    "return_statement",
    "break_statement",
    "continue_statement",
    "throw_statement",
    "yield_statement",
    "goto_statement",
}

DECLARATIONS = {
    "local_declaration_statement",
    "field_declaration",
    "property_declaration",  # `public int Count { get; set; }`
    "expression_statement",
    "local_function_statement",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

NESTED_SCOPES = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "struct_declaration",
    "record_declaration",
    "method_declaration",
    "constructor_declaration",
    "destructor_declaration",
    "operator_declaration",
    "local_function_statement",
    "lambda_expression",
    "anonymous_method_expression",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)

FRAMEWORKS = ["aspnet", "aspnetcore"]
