"""Flat statement capture for C# (gated by --capture-statements) + shared API/DB
call detection."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "invocation_expression"


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("local_declaration_statement", "field_declaration"):
        vd = next((c for c in node.named_children if c.type == "variable_declaration"), None)
        if vd is not None:
            decl = next((c for c in vd.named_children if c.type == "variable_declarator"), None)
            if decl is not None:
                nm = decl.child_by_field_name("name") or (
                    decl.named_children[0] if decl.named_children else None)
                if nm is not None:
                    return node_text(nm, source)
    elif node.type == "property_declaration":  # `public int Count { get; set; }` -> Count
        nm = node.child_by_field_name("name")
        if nm is not None:
            return node_text(nm, source)
    return None


def method_name(name_node: Node | None, source: bytes) -> str:
    """Method name from an invocation's ``function``/``name`` node, dropping generic
    type arguments (``GetById<Order>`` -> ``GetById``) so db/api classification and
    call-path matching key on the bare name."""
    if name_node is None:
        return ""
    if name_node.type == "generic_name":
        ident = next((c for c in name_node.named_children if c.type == "identifier"), None)
        if ident is not None:
            return node_text(ident, source)
    return node_text(name_node, source)


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    func = call.child_by_field_name("function")
    if func is None:
        return None
    if func.type == "member_access_expression":
        name_node = func.child_by_field_name("name")
        obj = func.child_by_field_name("expression")
        method = method_name(name_node, source)
        callee = f"{node_text(obj, source)}.{method}" if obj is not None else method
    else:
        method = method_name(func, source)
        callee = method
    first_str = None
    args = call.child_by_field_name("arguments")
    if args is not None:
        for arg in args.named_children:
            lit = arg if arg.type == "string_literal" else next(
                (c for c in arg.named_children if c.type == "string_literal"), None)
            if lit is not None:
                first_str = node_text(lit, source).strip('"')
                break
    return callee, method, first_str


def _iter_in_scope(node: Node, descend_all: bool = False):
    """Yield EMIT_TYPES statement nodes. ``descend_all=True`` (a function body) walks
    into inline lambdas/anonymous methods, attributing their statements to this function;
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
