"""Ruby AST node-type classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW ={
    "if_statement", 
    "unless_statement", 
    "while_statement", 
    "until_statement", 
    "for_statement", 
    "case_statement", 
    "begin"
    }

JUMP = {
    "return_statement",
     "break_statement", 
     "next",
     "redo", 
     "retry"
     }

DECLARATIONS = {
    "assignment", 
    "call",
    "alias"}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

NESTED_SCOPES = {
    "class", 
    "module", 
    "method", 
    "if_statement", 
    "while_statement", 
    "until_statement", 
    "for_statement", 
    "case_statement", 
    "begin"}

COMMENT_TYPES = {
    "comment"}

STATEMENT_TYPES = sorted(EMIT_TYPES)

FRAMEWORKS: list[str] = []
