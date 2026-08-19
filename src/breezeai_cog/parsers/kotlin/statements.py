"""Flat statement capture for Kotlin (gated by --capture-statements)."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement, render_concat, resolve_endpoint
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"


def _render_url(node: Node, source: bytes) -> str | None:
    if node.type == "string_literal":
        # Kotlin string literals: content is in string_content children
        parts = []
        for child in node.named_children:
            if child.type == "string_content":
                parts.append(node_text(child, source))
            elif child.type in {"string_interpolation", "multiline_string_interpolation"}:
                expr = next(iter(child.named_children), None)
                if expr is not None and expr.type == "simple_identifier":
                    parts.append("{" + node_text(expr, source) + "}")
                else:
                    parts.append("{param}")
        return "".join(parts) if parts else node_text(node, source).strip('"')
    if node.type == "additive_expression":
        return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    if call.type != "call_expression":
        return None
    callee_node = call.named_children[0] if call.named_children else None
    if callee_node is None:
        return None

    if callee_node.type == "navigation_expression":
        receiver = next(
            (node_text(c, source) for c in callee_node.named_children if c.type == "simple_identifier"),
            "",
        )
        suffix = next(
            (c for c in reversed(callee_node.named_children) if c.type == "navigation_suffix"),
            None,
        )
        method = ""
        if suffix is not None:
            m = next((c for c in suffix.named_children if c.type == "simple_identifier"), None)
            method = node_text(m, source) if m is not None else ""
        callee = f"{receiver}.{method}" if receiver else method
    else:
        method = node_text(callee_node, source) if callee_node.type == "simple_identifier" else ""
        callee = method

    # arguments: call_suffix → value_arguments → value_argument*
    args: list[Node] = []
    for child in call.named_children:
        if child.type == "call_suffix":
            val_args = next((c for c in child.named_children if c.type == "value_arguments"), None)
            if val_args is not None:
                args = [c for c in val_args.named_children if c.type == "value_argument"]
            break

    arg_nodes: list[Node] = [
        a for a in (next(iter(va.named_children), None) for va in args) if a is not None
    ]
    endpoint, override = resolve_endpoint(arg_nodes, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


def _iter_in_scope(
    node: Node,
    descend_all: bool = False,
    stop_at: frozenset[str] = frozenset(),
) -> Iterator[Node]:
    for child in node.named_children:
        if child.type in stop_at:
            continue
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES:
            yield child
        yield from _iter_in_scope(child, descend_all, stop_at)


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
    stop_at: frozenset[str] = frozenset(),
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all, stop_at):
        out.extend(
            classify_statement(
                node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
                emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=_CALL_TYPE,
                call_details=_call_details, language="kotlin",
            )
        )
    return out
