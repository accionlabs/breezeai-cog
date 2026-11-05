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
    # Drop the namespace qualifier, never a dot inside the type arguments:
    # `Ns.ExtendObjectType<Ns.Book>` → `ExtendObjectType<Ns.Book>`.
    base, lt, type_args = (node_text(name_node, source) if name_node is not None else "").partition("<")
    name = base.rsplit(".", 1)[-1] + lt + type_args
    args: list[str] = []
    arglist = next((c for c in node.named_children if c.type == "attribute_argument_list"), None)
    if arglist is not None:
        for arg in arglist.named_children:
            if arg.type != "attribute_argument":
                continue
            text = node_text(arg, source)
            # A positional string is kept unquoted (the route template, the name); a named
            # argument as written, key and all: `[HttpGet(Name = "GetOrders")]` names the route,
            # it is not its path.
            if arg.child_by_field_name("name") is None and len(arg.named_children) == 1 \
                    and arg.named_children[0].type == "string_literal":
                text = text.strip('"')
            args.append(text)
    return Decorator(name=name, args=args)


def extract_attributes(node: Node, source: bytes) -> list[Decorator]:
    """Attributes declared on a node (its ``attribute_list`` children)."""
    out: list[Decorator] = []
    for child in node.children:
        if child.type == "attribute_list":
            out.extend(_attribute(a, source) for a in child.named_children if a.type == "attribute")
    return out
