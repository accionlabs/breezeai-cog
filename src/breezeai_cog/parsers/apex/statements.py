"""Flat statement capture for Apex (gated by --capture-statements)."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...schemas import Decorator, Statement
from ..statements_common import classify_statement
from ..treesitter import node_text
from .detect import CALL_TYPES, classify_node
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES
from .modifiers import extract_annotations, modifiers_node


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("local_variable_declaration", "field_declaration"):
        decl = next((c for c in node.named_children if c.type == "variable_declarator"), None)
        name = decl.child_by_field_name("name") if decl is not None else None
        if name is not None:
            return node_text(name, source)
    return None


def _no_call_details(call: Node, source: bytes) -> None:
    return None  # classification is done per node by ``classify_node``


def _field_decorators(node: Node, source: bytes) -> list[Decorator] | None:
    if node.type != "field_declaration":
        return None
    return extract_annotations(modifiers_node(node), source)


def _iter_in_scope(node: Node) -> Iterator[Node]:
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
        out.extend(
            classify_statement(
                node, source, path, parent_id=parent_id, limit=limit, seen_ids=seen_ids,
                emit_types=EMIT_TYPES, control_flow=CONTROL_FLOW, call_type=CALL_TYPES,
                name_of=_name_of, call_details=_no_call_details, language="apex",
                classify=classify_node,
                decorators=_field_decorators(node, source),
            )
        )
    return out
