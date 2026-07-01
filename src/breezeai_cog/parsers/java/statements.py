"""Flat statement capture for Java (gated by --capture-statements) + shared API/DB
call detection."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ...utils import truncate
from ..detection import classify_call, text_has_query
from ..treesitter import first_line, node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in ("local_variable_declaration", "field_declaration"):
        decl = node.child_by_field_name("declarator")
        if decl is not None:
            name = decl.child_by_field_name("name")
            if name is not None:
                return node_text(name, source)
    return None


def _find_invocation(node: Node) -> Node | None:
    if node.type == "method_invocation":
        return node
    for child in node.named_children:
        if child.type in ("class_declaration", "method_declaration", "lambda_expression"):
            continue
        found = _find_invocation(child)
        if found is not None:
            return found
    return None


def _call_info(node: Node, source: bytes) -> tuple[str, str, str | None] | None:
    call = _find_invocation(node)
    if call is None:
        return None
    obj = call.child_by_field_name("object")
    name_node = call.child_by_field_name("name")
    method = node_text(name_node, source) if name_node is not None else ""
    callee = f"{node_text(obj, source)}.{method}" if obj is not None else method
    first_str = None
    args = call.child_by_field_name("arguments")
    if args is not None:
        for arg in args.named_children:
            if arg.type == "string_literal":
                first_str = node_text(arg, source).strip('"')
                break
    return callee, method, first_str


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
            text = first_line(text)
        start, col = node.start_point[0] + 1, node.start_point[1]

        semantic = method_value = endpoint = hint = None
        info = _call_info(node, source)
        if info is not None:
            classified = classify_call(info[0], info[1], info[2])
            if classified is not None:
                semantic, method_value, hint = classified
                if semantic == "api_call":
                    endpoint = info[2]
        if semantic is None and text_has_query(text):  # raw SQL string literal
            semantic = "query_statement"

        out.append(
            Statement(
                id=disambiguate(statement_id(path, start, col), seen_ids),
                parentId=parent_id,
                nodeType=node.type,
                semanticType=semantic,
                text=truncate(text, limit),
                name=_name_of(node, source),
                method=method_value,
                endpoint=endpoint,
                dataAccessHint=hint,
                startLine=start,
                endLine=node.end_point[0] + 1,
                path=path,
            )
        )
    return out
