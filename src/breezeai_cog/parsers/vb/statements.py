"""Flat statement capture for VB.NET (gated by --capture-statements) + shared API/DB
call detection.

VB wraps each in-body statement in a ``statement`` node and uses ``invocation`` /
``member_access`` (with ``target`` / ``object`` / ``member`` fields) rather than C#'s
``invocation_expression`` / ``member_access_expression``."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "invocation"


def _unwrap(node: Node) -> Node:
    """A ``statement`` wrapper holds exactly one real statement node."""
    if node.type == "statement" and node.named_children:
        return node.named_children[0]
    return node


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type == "dim_statement":
        nm = node.child_by_field_name("name")
        if nm is not None:
            return node_text(nm, source)
        decl = next((c for c in node.named_children if c.type == "variable_declarator"), None)
        if decl is not None and decl.named_children:
            return node_text(decl.named_children[0], source)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    target = call.child_by_field_name("target")
    if target is not None and target.type == "member_access":
        member = target.child_by_field_name("member")
        obj = target.child_by_field_name("object")
        method = node_text(member, source) if member is not None else ""
        callee = f"{node_text(obj, source)}.{method}" if obj is not None else method
    else:
        method = node_text(target, source) if target is not None else ""
        callee = method
    first_str = None
    args = call.child_by_field_name("arguments")
    if args is not None:
        for arg in args.named_children:
            lit = _find_string(arg)
            if lit is not None:
                first_str = node_text(lit, source).strip('"')
                break
    return callee, method, first_str


def _find_string(node: Node) -> Node | None:
    if node.type == "string_literal":
        return node
    for c in node.named_children:
        found = _find_string(c)
        if found is not None:
            return found
    return None


def _iter_in_scope(node: Node, descend_all: bool = False):
    """Yield EMIT_TYPES statement nodes. ``descend_all=True`` (a function body) walks
    into inline lambdas, attributing their statements to this function; ``False``
    (file-root / class-body) keeps nested scopes as barriers since they are extracted
    as their own Function/Class."""
    for child in node.named_children:
        real = _unwrap(child)
        if not descend_all and real.type in NESTED_SCOPES:
            continue
        if real.type in EMIT_TYPES:
            yield real
        yield from _iter_in_scope(real, descend_all)


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
                name_of=_name_of, call_details=_call_details, language="vb",
            )
        )
    return out
