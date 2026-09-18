"""Flat Ruby statement capture."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement, render_concat, resolve_endpoint
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, DECLARATIONS, EMIT_TYPES, JUMP

_SCOPE_TYPES = {"class", "module", "method"}


def _iter_statements(node: Node):
    for child in node.named_children:
        if child.type in EMIT_TYPES:
            yield child
        if child.type in _SCOPE_TYPES:
            continue
        yield from _iter_statements(child)


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type == "assignment":
        left = node.child_by_field_name("left")
        if left is not None:
            return node_text(left, source)
    if node.type in {"class", "module"}:
        constant = next((c for c in node.named_children if c.type == "constant"), None)
        if constant is not None:
            return node_text(constant, source)
    if node.type == "method":
        ident = next((c for c in node.named_children if c.type == "identifier"), None)
        if ident is not None:
            return node_text(ident, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    if node.type == "string":
        content = next((child for child in node.named_children if child.type == "string_content"), None)
        return node_text(content, source) if content is not None else ""
    if node.type == "binary":
        return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    receiver = call.child_by_field_name("receiver")
    method_node = call.child_by_field_name("method")
    method = node_text(method_node, source) if method_node is not None else ""
    callee = f"{node_text(receiver, source)}.{method}" if receiver is not None else method
    arguments = call.child_by_field_name("arguments")
    args = list(arguments.named_children) if arguments is not None else []
    endpoint, override = resolve_endpoint(args, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


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
    for node in _iter_statements(body):
        if node.type not in EMIT_TYPES:
            continue
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
                call_type="call",
                name_of=_name_of,
                call_details=_call_details,
                language="ruby",
            )
        )
    return out
