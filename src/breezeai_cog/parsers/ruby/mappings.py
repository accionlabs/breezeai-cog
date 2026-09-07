"""Ruby AST node-type classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW ={
    "if", 
    "unless", 
    "while", 
    "until", 
    "for", 
    "case", 
    "begin"
    }

JUMP = {
    "return",
     "break", 
     "next", 
     "redo", 
     "retry"
     }

DECLARATIONS = {
    "assignment", 
    "call", 
    "class", 
    "module", 
    "method", 
    "alias"}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

NESTED_SCOPES = {
    "class", 
    "module", 
    "method", 
    "if", 
    "while", 
    "until", 
    "for", 
    "case", 
    "begin"}

COMMENT_TYPES = {
    "comment"}

STATEMENT_TYPES = sorted(EMIT_TYPES)

FRAMEWORKS: list[str] = []
