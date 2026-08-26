"""TypeScript/JavaScript AST node-type classification + capability metadata."""

from __future__ import annotations

CONTROL_FLOW = {
    "if_statement",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "do_statement",
    "switch_statement",
    "try_statement",
    "catch_clause",  # error-handling boundary (bodies already captured; this adds the clause node)
    "finally_clause",
}

JUMP = {
    "return_statement",
    "break_statement",
    "continue_statement",
    "throw_statement",
}

DECLARATIONS = {
    "lexical_declaration",
    "variable_declaration",
    "expression_statement",
    "type_alias_declaration",  # TS `type X = …` (absent in JS grammar, so JS-safe)
    "public_field_definition",  # TS class field  `count: number = 0`
    "field_definition",  # JS class field  `count = 0`
}

EMIT_TYPES = CONTROL_FLOW | JUMP | DECLARATIONS

#: JS/TS built-in Array/String prototype methods. A call to one of these (`x.map()`,
#: `s.split()`) resolves to a runtime built-in, never an in-repo file — so it must not be
#: attributed to the receiver type's declaring module (fabricates a wrong CALLS edge). Only
#: low-collision names are listed: domain-ambiguous verbs (`get`/`set`/`has`/`delete`/`add`/
#: `find`/`create`/`update`/`count`/`index`/`search`) are deliberately excluded — a repo may
#: genuinely declare a method of that name, and dropping those edges would cost real recall.
JS_BUILTIN_METHODS = frozenset({
    # Array.prototype (iteration / query / mutation / transform)
    "map", "filter", "forEach", "reduce", "reduceRight", "some", "every",
    "flatMap", "flat", "concat", "slice", "splice", "fill", "copyWithin",
    "reverse", "sort", "includes", "indexOf", "lastIndexOf", "findIndex",
    "findLastIndex", "push", "pop", "shift", "unshift", "join", "at",
    # String.prototype
    "split", "trim", "trimStart", "trimEnd", "padStart", "padEnd", "repeat",
    "toLowerCase", "toUpperCase", "startsWith", "endsWith", "charAt",
    "charCodeAt", "codePointAt", "substring", "substr", "replaceAll",
    "matchAll", "normalize", "localeCompare",
})

#: Scopes whose inner statements belong to that nested scope.
NESTED_SCOPES = {
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "class_declaration",
    "class",
}

STATEMENT_TYPES = sorted(EMIT_TYPES)

#: Comment node types the shared comment pass captures (``semanticType="comment"``; each
#: statement keeps its real tree-sitter ``nodeType``).
COMMENT_TYPES = {"comment"}

FRAMEWORKS = [
    "angular", "nestjs", "loopback", "express", "react", "vue",
    # AWS messaging / Lambda (see aws_events.py) — transport carried on statement.framework.
    "aws-sns", "aws-sqs", "aws-eventbridge", "aws-lambda", "aws-apigw",
    "aws-dynamodb", "aws-kinesis", "aws-s3", "aws-ses",
]
