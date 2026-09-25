"""Ruby AST node-type classification + capability metadata."""

from __future__ import annotations

# These are the Ruby grammar's equivalents of the shared Branch / Loop / Exception
# / Jump / Leaf statement categories. Keep the real Tree-sitter node names here: the
# capture contract exposes nodeType, so substituting cross-language names would be false.
CONTROL_FLOW = {
    "if",                # Branch: if_statement / if_expression
    "unless",            # Branch: Ruby-specific inverse conditional
    "while",             # Loop: while_statement
    "until",             # Loop: Ruby-specific inverse loop
    "for",               # Loop: for_statement / for_in_statement
    "case",              # Branch: switch_statement / switch_expression
    "begin",              # Exception boundary (Ruby begin/rescue/ensure)
}

JUMP = {
    "return",            # return_statement
    "break",             # break_statement
    "next",              # continue_statement
    "redo",
    "retry",
}

DECLARATIONS = {
    "assignment",
    "call",               # Ruby has no expression_statement wrapper for bare calls
    "alias",
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

# Class/module/method bodies are separate ownership scopes. Control-flow nodes remain
# in this set for consumers that need the complete Ruby scope vocabulary, even though
# statement.py currently stops descent at class/module/method explicitly.
NESTED_SCOPES = {
    "class",
    "module",
    "method",
    *CONTROL_FLOW,
}

# Ruby's grammar exposes comments as `comment`; the other comment names in the shared
# spec belong to other grammars. `enum_declaration` is intentionally absent because Ruby
# enums are represented as ordinary classes/modules, not as Statement nodes.
COMMENT_TYPES = {"comment"}

STATEMENT_TYPES = sorted(EMIT_TYPES)

FRAMEWORKS: list[str] = []
