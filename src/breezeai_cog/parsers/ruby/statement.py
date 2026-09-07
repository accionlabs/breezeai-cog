"""Flat Ruby statement capture."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, DECLARATIONS, EMIT_TYPES, JUMP


def _iter_statements(node: Node):
    for child in node.named_children:
        if child.type in EMIT_TYPES:
            yield child
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
        start, col = node.start_point[0] + 1, node.start_point[1]
        end = node.end_point[0] + 1
        out.append(
            Statement(
                id=disambiguate(statement_id(path, start, col), seen_ids),
                parentId=parent_id,
                nodeType=node.type,
                text=node_text(node, source),
                name=_name_of(node, source),
                startLine=start,
                endLine=end,
                path=path,
            )
        )
    return out
