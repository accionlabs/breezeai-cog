"""Flat statement capture for PHP (gated by --capture-statements) + shared API/DB detection."""

from __future__ import annotations

from collections.abc import Iterator

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
from .attributes import extract_attributes
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPES = (
    "member_call_expression",
    "scoped_call_expression",
    "function_call_expression",
    "nullsafe_member_call_expression",
)


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type == "property_declaration":
        for child in node.named_children:
            if child.type == "property_element":
                nm = child.child_by_field_name("name")
                if nm is not None:
                    return node_text(nm, source).lstrip("$")
    elif node.type == "global_declaration":
        first_var = next((c for c in node.named_children if c.type == "variable_name"), None)
        if first_var is not None:
            return node_text(first_var, source).lstrip("$")
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    """Best-effort URL/path from a PHP string, encapsed string (\"/users/{$id}\"), or . concatenation."""
    if node.type == "argument":
        inner = node.named_children[0] if node.named_children else None
        return _render_url(inner, source) if inner is not None else None
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_content"), None)
        return node_text(frag, source) if frag is not None else node_text(node, source).strip("'\"")
    if node.type == "encapsed_string":
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_content":
                parts.append(node_text(c, source))
            else:
                parts.append(url_placeholder(node_text(c, source).lstrip("$")))
        return strip_leading_base("".join(parts))
    if node.type == "binary_expression":  # '/users/' . $id
        return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    if call.type in ("member_call_expression", "nullsafe_member_call_expression"):
        obj = call.child_by_field_name("object")
        name_node = call.child_by_field_name("name")
        method = node_text(name_node, source) if name_node is not None else ""
        obj_text = node_text(obj, source) if obj is not None else ""
        callee = f"{obj_text}->{method}" if obj_text else method
    elif call.type == "scoped_call_expression":
        scope = call.child_by_field_name("scope")
        name_node = call.child_by_field_name("name")
        method = node_text(name_node, source) if name_node is not None else ""
        scope_text = node_text(scope, source) if scope is not None else ""
        callee = f"{scope_text}::{method}" if scope_text else method
    elif call.type == "function_call_expression":
        fn = call.child_by_field_name("function")
        method = node_text(fn, source) if fn is not None else ""
        callee = method
    else:
        return None

    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    endpoint, override = resolve_endpoint(named, source, _render_url)
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
    """Yield EMIT_TYPES statement nodes. When descend_all=True (function body or whole-body
    walk), walks into inline closures and lambdas without crossing named function/class barriers."""
    for child in node.named_children:
        if _span(child) in barriers:
            continue
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES:
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
                call_type=_CALL_TYPES,
                name_of=_name_of,
                call_details=_call_details,
                language="php",
                decorators=extract_attributes(node, source),
            )
        )
    return out
