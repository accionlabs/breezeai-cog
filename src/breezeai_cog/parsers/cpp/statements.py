"""Flat statement capture for C++ (gated by --capture-statements) + shared API/DB
call detection.

C++ has comparatively few HTTP/DB calls, but detection is wired the same as every
other language so an HTTP-client or DB call is still classified when present."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement, render_concat, resolve_endpoint
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a string literal or ``+`` concatenation. A non-string
    part becomes ``{name}`` via the shared placeholder logic."""
    if node.type == "string_literal":
        frag = next((c for c in node.named_children if c.type == "string_content"), None)
        return node_text(frag, source) if frag is not None else node_text(node, source).strip('"')
    if node.type == "concatenated_string":  # adjacent literals: "a" "b"
        parts = [_render_url(c, source) for c in node.named_children]
        joined = "".join(p for p in parts if p is not None)
        return joined or None
    if node.type == "binary_expression":  # "/users/" + id
        return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "field_expression":
        obj = fn.child_by_field_name("argument")
        field = fn.child_by_field_name("field")
        method = node_text(field, source) if field is not None else ""
        callee = f"{node_text(obj, source)}.{method}" if obj is not None else method
    elif fn.type == "qualified_identifier":
        name = fn.child_by_field_name("name")
        method = node_text(name, source) if name is not None else ""
        callee = node_text(fn, source)
    else:
        method = node_text(fn, source)
        callee = method
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    endpoint, override = resolve_endpoint(named, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _iter_in_scope(node: Node, descend_all: bool = False) -> Iterator[Node]:
    """Yield EMIT_TYPES statement nodes. ``descend_all=True`` (a function body) walks into
    inline lambdas, attributing their statements to this function; ``False`` (file/class
    scope) keeps nested scopes as barriers since they are extracted as their own
    Function/Class."""
    for child in node.named_children:
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES:
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
                call_details=_call_details, language="cpp",
            )
        )
    return out
