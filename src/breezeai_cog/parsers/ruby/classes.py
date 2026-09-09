"""Ruby class / module extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, Function, Statement
from ..treesitter import line_span, node_text
from .functions import build_function
from .statement import extract_statements


def _name_of_class(node: Node, source: bytes) -> str:
    constant = next((c for c in node.named_children if c.type == "constant"), None)
    if constant is not None:
        return node_text(constant, source)
    return "<anonymous>"


def _body_of(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "body_statement"), None)


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve,
):
    name = _name_of_class(node, source)
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    body = _body_of(node)
    methods: list[Function] = []
    statements: list[Statement] = []
    nested_classes: list[Class] = []

    if body is not None:
        statements.extend(extract_statements(body, source, path, parent_id=cid, capture=capture, limit=limit, seen_ids=seen_ids))
        for child in body.named_children:
            if child.type == "method":
                fns, fn_stmts = build_function(
                    child,
                    source,
                    path,
                    parent_id=cid,
                    class_name=name,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                )
                methods.extend(fns)
                statements.extend(fn_stmts)
            elif child.type in {"class", "module"}:
                sub_classes, sub_methods, sub_stmts = build_class(
                    child,
                    source,
                    path,
                    parent_id=cid,
                    seen_ids=seen_ids,
                    capture=capture,
                    limit=limit,
                    resolve=resolve,
                )
                nested_classes.extend(sub_classes)
                methods.extend(sub_methods)
                statements.extend(sub_stmts)

    cls = Class(
        id=cid,
        parentId=parent_id,
        path=path,
        name=name,
        type="class" if node.type == "class" else "module",
        visibility="public",
        startLine=start,
        endLine=end,
    )
    return [cls, *nested_classes], methods, statements


def iter_definitions(root: Node):
    for child in root.named_children:
        if child.type in {"class", "module", "method"}:
            yield child
        else:
            yield from iter_definitions(child)
