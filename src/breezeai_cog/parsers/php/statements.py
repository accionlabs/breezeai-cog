"""Flat PHP statement extraction and shared call classification."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement, resolve_endpoint
from ..php_route_common import string_value
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPES = {
    "function_call_expression",
    "member_call_expression",
    "nullsafe_member_call_expression",
    "scoped_call_expression",
}
_SCOPE_BARRIERS = NESTED_SCOPES


def _walk_in_scope(node: Node) -> Iterator[Node]:
    for child in node.named_children:
        if child.type in _SCOPE_BARRIERS:
            continue
        if child.type in EMIT_TYPES:
            yield child
        yield from _walk_in_scope(child)


def _unwrap(node: Node) -> Node:
    if node.type == "argument" and len(node.named_children) == 1:
        return node.named_children[0]
    return node


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    if call.type == "function_call_expression":
        function = call.child_by_field_name("function")
        if function is None:
            return None
        callee = node_text(function, source)
        method = callee.rsplit("\\", 1)[-1]
    else:
        method_node = call.child_by_field_name("name")
        if method_node is None:
            return None
        method = node_text(method_node, source)
        if call.type == "scoped_call_expression":
            receiver = call.child_by_field_name("scope")
            separator = "::"
        else:
            receiver = call.child_by_field_name("object")
            separator = "->"
        callee = f"{node_text(receiver, source)}{separator}{method}" if receiver is not None else method
        callee = callee.replace("::", ".").replace("->", ".")
    args_node = call.child_by_field_name("arguments")
    args = [_unwrap(arg) for arg in args_node.named_children] if args_node is not None else []
    endpoint, override = resolve_endpoint(args, source, string_value)
    return callee, override or method, endpoint


def _name_of(node: Node, source: bytes) -> str | None:
    for child in node.named_children:
        if child.type in {"property_element", "const_element"}:
            name = child.child_by_field_name("name")
            if name is None and child.type == "const_element":
                name = next(
                    (item for item in child.named_children if item.type == "name"),
                    None,
                )
            if name is not None:
                return node_text(name, source).lstrip("$")
    if node.type == "expression_statement":
        expr = node.named_children[0] if node.named_children else None
        if expr is not None and expr.type == "assignment_expression":
            left = expr.child_by_field_name("left")
            if left is not None:
                return node_text(left, source).lstrip("$")
    return None


def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _walk_in_scope(body):
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
            )
        )
    return out
