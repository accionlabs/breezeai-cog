"""Python AST node-type → statement classification + capability metadata.

``STATEMENT_TYPES`` is the source of the parser's capability-discovery list.
Semantic detection (route / db / api / event) is added in M4 via ``parsers/detection``.
"""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "while_statement",
    "try_statement",
    "with_statement",
    "match_statement",
}

JUMP = {
    "return_statement",
    "break_statement",
    "continue_statement",
    "raise_statement",
    "pass_statement",
}

DECLARATIONS = {
    "assignment",
    "augmented_assignment",
    "expression_statement",
    "global_statement",
    "nonlocal_statement",
    "delete_statement",
    "assert_statement",
}

#: Node types emitted as Statements (flat) when --capture-statements is on.
EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

#: Scopes whose inner statements belong to that nested scope, not the enclosing one.
NESTED_SCOPES = {"function_definition", "class_definition", "decorated_definition"}

STATEMENT_TYPES = sorted(EMIT_TYPES)

FRAMEWORKS = ["fastapi", "flask", "django"]
