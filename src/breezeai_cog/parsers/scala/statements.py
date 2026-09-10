"""Flat statement capture for Scala (gated by --capture-statements).

Emits one Statement per matching node at every depth within the same scope. A
statement that contains a call is run through the shared detectors
(``parsers/detection``) to set ``semanticType`` (api_call / db_method_call / query_statement)
+ ``method`` / ``endpoint`` / ``dataAccessHint`` on the same span.
"""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...schemas import FileRecord, Statement
from ..statements_common import (
    classify_statement,
    render_concat,
    resolve_endpoint,
    strip_leading_base,
    url_placeholder,
)
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"

# Bare expression-statements: Scala puts a statement-position call / infix expression
# directly under a block or template_body (no expression_statement wrapper).
_STMT_EXPR = ("call_expression", "infix_expression")
#: ``compilation_unit`` is the file root: a script (.sc / .mill) or a top-level
#: statement in a .scala file puts a bare call directly under it, with no enclosing
#: block. Omitting it drops most of what a script file actually contains.
_CONTAINERS = (
    "compilation_unit",
    "block",
    "indented_block",
    "template_body",
    "with_template_body",
)


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("val_definition", "var_definition"):
        pat = node.child_by_field_name("pattern")
        if pat is not None and pat.type == "identifier":
            return node_text(pat, source)
    elif node.type == "assignment_expression":
        left = node.child_by_field_name("left")
        if left is None and node.named_children:
            left = node.named_children[0]
        if left is not None and left.type == "identifier":
            return node_text(left, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string, interpolated string (s"...", uri"..."), or + concat."""
    if node.type == "string":
        txt = node_text(node, source)
        if txt.startswith('"""') and txt.endswith('"""'):
            return txt[3:-3]
        return txt.strip('"')
    if node.type == "interpolated_string_expression":
        parts: list[str] = []
        for c in node.children:
            if not c.is_named:
                continue
            if c.type == "interpolated_string_text":
                parts.append(node_text(c, source))
            elif c.type == "identifier" and c.prev_sibling is None:
                # The interpolator prefix (e.g. 's' or 'uri')
                continue
            else:
                expr = c.named_children[0] if c.named_children else c
                parts.append(url_placeholder(node_text(expr, source)))
        return strip_leading_base("".join(parts))
    if node.type == "infix_expression":
        op = node.child_by_field_name("operator")
        if op is not None and node_text(op, source) == "+":
            return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    callee = node_text(fn, source)
    # If generic_function like HttpRoutes.of[IO], strip type parameters for method name
    raw_method = callee.rsplit(".", 1)[-1]
    if "[" in raw_method:
        raw_method = raw_method.split("[", 1)[0]
    method = raw_method

    args_node = call.child_by_field_name("arguments")
    named_args: list[Node] = []
    if args_node is not None:
        if args_node.type == "arguments":
            named_args = list(args_node.named_children)
        else:
            named_args = [args_node]
    endpoint, override = resolve_endpoint(named_args, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _span(node: Node) -> tuple[int, int]:
    return (node.start_byte, node.end_byte)


def _iter_in_scope(
    node: Node,
    descend_all: bool = False,
    barriers: frozenset[tuple[int, int]] = frozenset(),
) -> Iterator[Node]:
    for child in node.named_children:
        if _span(child) in barriers:
            continue
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES or (child.type in _STMT_EXPR and node.type in _CONTAINERS):
            yield child
        yield from _iter_in_scope(child, descend_all, barriers)


def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
    descend_all: bool = False,
    barriers: frozenset[tuple[int, int]] = frozenset(),
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all, barriers):
        out.extend(
            classify_statement(
                node,
                source,
                path,
                parent_id=parent_id,
                limit=limit,
                seen_ids=seen_ids,
                emit_types=EMIT_TYPES,
                control_flow=CONTROL_FLOW,
                call_type=_CALL_TYPE,
                name_of=_name_of,
                call_details=_call_details,
                stmt_expr=_STMT_EXPR,
                container_types=_CONTAINERS,
                language="scala",
            )
        )
    return out


def find_enclosing_parent_id(start_line: int, record: FileRecord) -> str:
    """Find the ID of the smallest function or class enclosing start_line, or fallback to record.id."""
    fn_candidates = [
        f for f in record.functions
        if f.startLine <= start_line <= f.endLine
    ]
    if fn_candidates:
        fn_candidates.sort(key=lambda f: (f.endLine - f.startLine, -f.startLine))
        return fn_candidates[0].id
    cls_candidates = [
        c for c in record.classes
        if c.startLine <= start_line <= c.endLine
    ]
    if cls_candidates:
        cls_candidates.sort(key=lambda c: (c.endLine - c.startLine, -c.startLine))
        return cls_candidates[0].id
    return record.id
