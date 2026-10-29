"""Flat PHP statement extraction and shared call classification."""

from __future__ import annotations

from collections.abc import Iterator
import re

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement, resolve_endpoint, url_first
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
#: Closures: a Function only when stored in a variable (``$notify = function …``).
CLOSURES = frozenset({"anonymous_function", "arrow_function"})


def bound_closure_name(node: Node, source: bytes) -> str | None:
    """The variable a closure is stored in (``$notify = function ($m) {…};`` → ``notify``),
    else None — an inline closure (an argument, an array value, a property assignment)."""
    parent = node.parent
    if node.type not in CLOSURES or parent is None or parent.type != "assignment_expression":
        return None
    statement = parent.parent  # the assignment is the whole statement, not part of an expression
    if statement is None or statement.type != "expression_statement" or len(statement.named_children) != 1:
        return None
    right, left = parent.child_by_field_name("right"), parent.child_by_field_name("left")
    if right is None or (right.start_byte, right.end_byte) != (node.start_byte, node.end_byte):
        return None
    if left is None or left.type != "variable_name":
        return None
    return node_text(left, source).lstrip("$") or None


def _stores_only(node: Node, bound: frozenset[tuple[int, int]]) -> bool:
    """True for ``$notify = function …;`` whose closure is a Function (in ``bound``): the
    statement would repeat it."""
    if not bound or node.type != "expression_statement" or len(node.named_children) != 1:
        return False
    expr = node.named_children[0]
    right = expr.child_by_field_name("right") if expr.type == "assignment_expression" else None
    return right is not None and (right.start_byte, right.end_byte) in bound


def _walk_in_scope(node: Node, bound: frozenset[tuple[int, int]] = frozenset()) -> Iterator[Node]:
    """Statement nodes of a scope. The walk stops at classes and declared functions and at a
    closure stored in a variable (``bound``), each its own record; an inline closure is
    walked, so its statements belong to the enclosing function, or to the file."""
    for child in node.named_children:
        span = (child.start_byte, child.end_byte)
        if (child.type in _SCOPE_BARRIERS and child.type not in CLOSURES) or span in bound:
            continue
        if _stores_only(child, bound):
            continue
        if child.type in EMIT_TYPES:
            yield child
        yield from _walk_in_scope(child, bound)


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
    endpoint, override = resolve_endpoint(args, source, string_value, method=url_first(callee, method))
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
    bound: frozenset[tuple[int, int]] = frozenset(),
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _walk_in_scope(body, bound):
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


def extract_expression_body(
    body: Node, source: bytes, path: str, *, parent_id: str, capture: bool, limit: int,
    seen_ids: set[str],
) -> list[Statement]:
    """The expression body of a stored arrow function (``$f = fn ($x) => save($x);``) as that
    function's one statement, with its own raw ``nodeType``."""
    if not capture:
        return []
    return classify_statement(
        body, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
        emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=_CALL_TYPES,
        name_of=_name_of, call_details=_call_details, language="php",
    )
