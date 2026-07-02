"""Flat statement capture for TypeScript/JavaScript (gated by --capture-statements),
with shared API/DB call detection (``parsers/detection``)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ...utils import truncate
from ..detection import classify_call, text_has_query
from ..treesitter import first_line, node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES


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


def _find_call(node: Node) -> Node | None:
    if node.type == "call_expression":
        return node
    for child in node.named_children:
        if child.type in (
            "function_declaration", "function_expression", "arrow_function",
            "method_definition", "class_declaration",
        ):
            continue
        found = _find_call(child)
        if found is not None:
            return found
    return None


def _call_info(node: Node, source: bytes) -> tuple[str, str, str | None] | None:
    call = _find_call(node)
    if call is None:
        return None
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
