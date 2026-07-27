"""Groovy AST node-type classification + capability metadata.

Groovy structurally mirrors Java (package/import/class/method/field/enum), so the
node sets mirror Java's — but the dekobon grammar names a few nodes differently
(``for_in_statement``, ``super_interfaces``, ``trait_declaration``, ``closure``).
Groovy is a **best-effort** language: expression bodies with named-argument commas,
parenthesised enum constants, and heavy GString/DSL use degrade to *missing* nodes
(never wrong) — see ``.todo/groovy-grammar-evaluation.md``.
"""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "switch_expression",
    "try_statement",
}

JUMP = {
    "return_statement",
    "break_statement",
    "continue_statement",
    "throw_statement",
    "assert_statement",
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
    "trait_declaration",
    "method_declaration",
    "constructor_declaration",
    "closure",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)

FRAMEWORKS: list[str] = []
