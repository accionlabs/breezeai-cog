"""Flat statement capture (gated by --capture-statements).

Emits one Statement per matching node at every depth *within the same scope*. A
statement that contains a call is run through the shared detectors
(``parsers/detection``) to set ``semanticType`` (api_call / db_method_call) +
``method`` / ``endpoint`` / ``dataAccessHint`` on the same span.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call"


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("assignment", "augmented_assignment"):
        lhs = node.named_children[0] if node.named_children else None
        if lhs is not None and lhs.type == "identifier":
            return node_text(lhs, source)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    args = call.child_by_field_name("arguments")
    first_str = None
    if args is not None:
        for arg in args.named_children:
            if arg.type == "string":
                content = next((c for c in arg.named_children if c.type == "string_content"), None)
                first_str = node_text(content, source) if content is not None else None
                break
    return callee, callee.rsplit(".", 1)[-1], first_str


def _iter_in_scope(node: Node, descend_all: bool = False):
    """Yield EMIT_TYPES statement nodes. ``descend_all=True`` (a function body) walks
    into inline lambdas and nested defs, attributing their statements to this function;
    ``False`` (file-root / class-body) keeps nested scopes as barriers since they are
    extracted as their own Function/Class."""
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
                name_of=_name_of, call_details=_call_details,
            )
        )
    return out
