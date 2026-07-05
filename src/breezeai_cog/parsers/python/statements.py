"""Flat statement capture (gated by --capture-statements).

Emits one Statement per matching node at every depth *within the same scope*. A
statement that contains a call is run through the shared detectors
(``parsers/detection``) to set ``semanticType`` (api_call / db_method_call) +
``method`` / ``endpoint`` / ``dataAccessHint`` on the same span.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import (
    classify_statement,
    render_concat,
    resolve_endpoint,
    strip_leading_base,
    url_placeholder,
)
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call"
# Bare expression-statements: this grammar puts a statement-position call/await
# directly under a block (no ``expression_statement`` wrapper), so they'd otherwise
# be dropped. ``_CONTAINERS`` are the nodes that hold statements directly, used to
# tell a statement-position call from a call nested inside an expression.
_STMT_EXPR = ("call", "await")
_CONTAINERS = ("block", "module")


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("assignment", "augmented_assignment"):
        lhs = node.named_children[0] if node.named_children else None
        if lhs is not None and lhs.type == "identifier":
            return node_text(lhs, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string, f-string, or ``+`` concatenation. f-string
    interpolations become ``{name}`` placeholders; a leading interpolated base is dropped."""
    if node.type == "string":  # plain or f-string (interpolations are child nodes)
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_content":
                parts.append(node_text(c, source))
            elif c.type == "interpolation":
                expr = c.named_children[0] if c.named_children else None
                parts.append(url_placeholder(node_text(expr, source)) if expr is not None else "{param}")
        return strip_leading_base("".join(parts))
    if node.type == "binary_operator":  # string concatenation: '/a/' + str(id)
        return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    method = callee.rsplit(".", 1)[-1]
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    endpoint, override = resolve_endpoint(named, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _iter_in_scope(node: Node, descend_all: bool = False):
    """Yield EMIT_TYPES statement nodes. ``descend_all=True`` (a function body) walks
    into inline lambdas and nested defs, attributing their statements to this function;
    ``False`` (file-root / class-body) keeps nested scopes as barriers since they are
    extracted as their own Function/Class."""
    for child in node.named_children:
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES or (child.type in _STMT_EXPR and node.type in _CONTAINERS):
            yield child
        yield from _iter_in_scope(child, descend_all)


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
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all):
        out.extend(
            classify_statement(
                node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
                emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=_CALL_TYPE,
                name_of=_name_of, call_details=_call_details,
                stmt_expr=_STMT_EXPR, container_types=_CONTAINERS, language="python",
            )
        )
    return out
