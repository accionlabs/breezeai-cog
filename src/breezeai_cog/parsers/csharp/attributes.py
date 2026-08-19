"""Attribute extraction (C#) — a leaf shared by the function, class, import, and
statement parsers. Depends only on the AST and the ``Decorator`` schema, so it
imports no sibling parser and stays free of the functions ⇄ statements cycle.

C# attributes hang off ``attribute_list`` children of a declaration."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Decorator
from ..treesitter import node_text


def _attribute(node: Node, source: bytes) -> Decorator:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source).rsplit(".", 1)[-1] if name_node is not None else ""
    args: list[str] = []
    arglist = next((c for c in node.named_children if c.type == "attribute_argument_list"), None)
    if arglist is not None:
        for arg in arglist.named_children:
            if arg.type != "attribute_argument":
                continue
            text = node_text(arg, source)
            inner = next((c for c in arg.named_children if c.type == "string_literal"), None)
            if inner is not None:
                text = node_text(inner, source).strip('"')
            args.append(text)
    return Decorator(name=name, args=args)


def extract_attributes(node: Node, source: bytes) -> list[Decorator]:
    """Attributes declared on a node (its ``attribute_list`` children)."""
    out: list[Decorator] = []
    for child in node.children:
        if child.type == "attribute_list":
            out.extend(_attribute(a, source) for a in child.named_children if a.type == "attribute")
    return out
