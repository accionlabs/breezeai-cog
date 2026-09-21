"""Flat statement capture for Go (gated by --capture-statements)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Decorator, Statement
from ..statements_common import classify_statement, render_concat, resolve_endpoint
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in {"var_declaration", "const_declaration"}:
        for child in node.named_children:
            if child.type == "identifier":
                return node_text(child, source)
    if node.type == "short_var_declaration":
        left = node.child_by_field_name("left")
        if left is not None:
            for child in left.named_children:
                if child.type == "identifier":
                    return node_text(child, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    if node.type == "interpreted_string_literal":
        text = node_text(node, source)
        return text.strip('"')
    if node.type == "raw_string_literal":
        text = node_text(node, source)
        return text.strip('`')
    if node.type == "binary_expression":
        return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    if call.type != "call_expression":
        return None
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "selector_expression":
        operand = fn.child_by_field_name("operand")
        field = fn.child_by_field_name("field")
        callee = f"{node_text(operand, source)}.{node_text(field, source)}" if operand is not None and field is not None else node_text(fn, source)
        method = node_text(field, source) if field is not None else ""
    else:
        callee = node_text(fn, source)
        method = callee
    args = call.child_by_field_name("arguments")
    arg_nodes: list[Node] = list(args.named_children) if args is not None else []
    endpoint, override = resolve_endpoint(arg_nodes, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _iter_in_scope(node: Node, descend_all: bool = False) -> list[Node]:
    out: list[Node] = []
    for child in node.named_children:
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES:
            out.append(child)
        out.extend(_iter_in_scope(child, descend_all))
    return out


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
                language="go",
                decorators=[],
            )
        )
    return out
