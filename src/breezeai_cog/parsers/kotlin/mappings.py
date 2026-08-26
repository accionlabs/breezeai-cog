"""Kotlin AST node-type classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_expression",
    "when_expression",
    "for_statement",
    "while_statement",
    "do_while_statement",
    "try_expression",
    "catch_block",  # error-handling boundary (bodies already captured; this adds the clause node)
    "finally_block",
}

JUMP = {
    "jump_expression",   # return / break / continue / throw
}

DECLARATIONS = {
    "property_declaration",
    "expression_statement",
    "call_expression",   # bare call at statement level
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

NESTED_SCOPES = {
    "class_declaration",
    "object_declaration",
    "companion_object",
    "function_declaration",
    "lambda_literal",
    "anonymous_initializer",
    "secondary_constructor",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)

#: Comment node types the shared comment pass captures (``semanticType="comment"``; each
#: statement keeps its real tree-sitter ``nodeType``).
COMMENT_TYPES = {"line_comment", "multiline_comment"}

FRAMEWORKS = ["ktor"]
