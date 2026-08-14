"""C++ AST node-type classification + capability metadata.

Node types are from the tree-sitter ``cpp`` grammar (language pack). C++ is a
**best-effort** language: real translation units routinely parse with ``has_error``
(macros like ``Q_OBJECT``, unexpanded preprocessor tokens, heavy template
metaprogramming) that degrade a declaration into an ``ERROR`` node. The extractors
guard every declaration with :func:`..functions.has_declaration_error` so a corrupt
header is skipped rather than turned into a fabricated node (absent beats wrong).
"""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "for_range_loop",
    "while_statement",
    "do_statement",
    "switch_statement",
    "try_statement",
}

JUMP = {
    "return_statement",
    "co_return_statement",
    "break_statement",
    "continue_statement",
    "throw_statement",
    "goto_statement",
}

DECLARATIONS = {
    "declaration",
    "expression_statement",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

#: Scopes that are extracted as their own Function/Class (or flattened, for a
#: namespace) — a barrier for file/class-level statement capture. A function body
#: still descends into ``lambda_expression`` (``descend_all=True``) so a lambda's
#: calls/statements attribute to the enclosing function.
NESTED_SCOPES = {
    "function_definition",
    "class_specifier",
    "struct_specifier",
    "union_specifier",
    "enum_specifier",
    "namespace_definition",
    "lambda_expression",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)

#: Comment node types the shared comment pass captures (``semanticType="comment"``; each
#: statement keeps its real tree-sitter ``nodeType``).
COMMENT_TYPES = {"comment"}

FRAMEWORKS: list[str] = []
