"""Flat statement capture for TypeScript/JavaScript (gated by --capture-statements),
with shared API/DB call detection (``parsers/detection``)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type == "lexical_declaration":
        decl = next((c for c in node.named_children if c.type == "variable_declarator"), None)
        if decl is not None:
            name = decl.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                return node_text(name, source)
    elif node.type in ("type_alias_declaration", "public_field_definition", "field_definition"):
        # `type X = …` -> X ;  class field `count = 0` -> count
        name = node.child_by_field_name("name")
        if name is not None:
            return node_text(name, source)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    fn = call.child_by_field_name("function")
    callee = node_text(fn, source) if fn is not None else ""
    args = call.child_by_field_name("arguments")
    first_str = None
    if args is not None:
        for arg in args.named_children:
            if arg.type == "string":
                frag = next((c for c in arg.named_children if c.type == "string_fragment"), None)
                first_str = node_text(frag, source) if frag is not None else None
                break
    return callee, callee.rsplit(".", 1)[-1], first_str


def _iter_in_scope(node: Node, descend_all: bool = False):
    """Yield EMIT_TYPES statement nodes. When ``descend_all`` is False (file-root /
    class-body scope) nested scopes remain barriers — they are extracted as their own
    Function/Class. When True (a function body) we descend into inline callbacks,
    lambdas and any nested scope, attributing their statements to this function — a
    function body never contains a separately-extracted scope, so there is no
    double-emit (see build_function). This closes the "callback black hole"."""
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
