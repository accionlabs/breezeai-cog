"""Decorator extraction (TS/JS) — a leaf shared by the function, class, and
statement parsers. Depends only on the AST and the ``Decorator`` schema, so it
imports no sibling parser and stays free of the functions ⇄ statements cycle."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Decorator
from ..treesitter import node_text


def decorator(node: Node, source: bytes) -> Decorator:
    inner = node.named_children[0] if node.named_children else None
    if inner is None:
        return Decorator(name=node_text(node, source).lstrip("@"), args=[])
    args: list[str] = []
    if inner.type == "call_expression":
        arglist = inner.child_by_field_name("arguments")
        if arglist is not None:
            args = [node_text(a, source) for a in arglist.named_children]
        inner = inner.child_by_field_name("function") or inner
    return Decorator(name=node_text(inner, source).rsplit(".", 1)[-1], args=args)


def extract_decorators(nodes: list[Node], source: bytes) -> list[Decorator]:
    return [decorator(n, source) for n in nodes]
