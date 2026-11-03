"""C and C++ AST node-type classification + capability metadata.

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
    "catch_clause",  # error-handling boundary (C++ has no finally)
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

#: Wrappers whose *members* are top-level declarations: the ``#ifndef GUARD`` include guard
#: every header uses (``preproc_ifdef``), other preprocessor conditionals, ``extern "C"``
#: (``linkage_specification``) and the braces of ``extern "C" { … }`` (``declaration_list``;
#: a namespace body is walked through ``namespace_definition``, never reached here). They are
#: walked even when they contain an error — a guard whose ``#ifdef __cplusplus`` blocks split
#: ``extern "C" {`` from its ``}`` always does — and each member is checked on its own.
TRANSPARENT_SCOPES = frozenset({
    "preproc_ifdef", "preproc_if", "preproc_ifndef", "preproc_else", "preproc_elif",
    "linkage_specification", "declaration_list",
})

#: Macro definitions (``#define NAME value``, ``#define MIN(a, b) …``): at file scope each is a
#: plain statement — constants and macro APIs (C's ``lua_pop``) live there, nowhere else.
MACRO_TYPES = frozenset({"preproc_def", "preproc_function_def"})

#: Extensions of files that are included rather than compiled on their own. A ``static``
#: function in one of them is visible to every file that includes it; in any other file it is
#: visible to that file only, so it never resolves a call from another file.
HEADER_EXTENSIONS = frozenset({".h", ".hh", ".hpp", ".hxx", ".h++", ".ipp", ".inl"})

#: Comment node types the shared comment pass captures (``semanticType="comment"``; each
#: statement keeps its real tree-sitter ``nodeType``).
COMMENT_TYPES = {"comment"}

FRAMEWORKS: list[str] = []
