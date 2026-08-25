"""Scala node-type mappings for statements, control flow, and framework discovery."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_expression",
    "for_expression",
    "while_expression",
    "do_while_expression",
    "try_expression",
    "match_expression",
}
JUMP = {"return_expression", "throw_expression"}
DECLARATIONS = {"val_definition", "var_definition", "assignment_expression"}
EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS
NESTED_SCOPES = {
    "class_definition",
    "object_definition",
    "trait_definition",
    "enum_definition",
    "function_definition",
    "function_declaration",
    "given_definition",
    "extension_definition",
}

#: Comment node types for the shared comment pass (§6.9). Verified in §3.3 — Scala uses
#: `comment` for `//` and `block_comment` for `/* */` and `/** */`.
COMMENT_TYPES = {"comment", "block_comment"}

STATEMENT_TYPES = sorted(EMIT_TYPES)
FRAMEWORKS = ["play", "akka-http", "http4s", "spark"]
