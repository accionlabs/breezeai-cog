"""Flat statement capture (gated by --capture-statements).

Emits one Statement per matching node at every depth *within the same scope*
(nested function/class bodies belong to their own scope). ``semanticType`` stays
``None`` — route/db/api/event detection arrives in M4.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ...utils import truncate
from ..treesitter import first_line, node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("assignment", "augmented_assignment"):
        lhs = node.named_children[0] if node.named_children else None
        if lhs is not None and lhs.type == "identifier":
            return node_text(lhs, source)
    return None


def _iter_in_scope(node: Node):
    for child in node.named_children:
        if child.type in NESTED_SCOPES:
            continue
        if child.type in EMIT_TYPES:
            yield child
        yield from _iter_in_scope(child)


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
    for node in _iter_in_scope(body):
        text = node_text(node, source)
        if node.type in CONTROL_FLOW:
            text = first_line(text)  # header only, not the whole block
        start, col = node.start_point[0] + 1, node.start_point[1]
        out.append(
            Statement(
                id=disambiguate(statement_id(path, start, col), seen_ids),
                parentId=parent_id,
                nodeType=node.type,
                text=truncate(text, limit),
                name=_name_of(node, source),
                startLine=start,
                endLine=node.end_point[0] + 1,
                path=path,
            )
        )
    return out
