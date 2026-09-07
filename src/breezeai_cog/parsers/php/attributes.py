"""PHP 8 attribute extraction (#[Route(...)] etc.) -> list[Decorator]."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Decorator
from ..treesitter import node_text


def _extract_decorator_from_attribute(attr: Node, source: bytes) -> Decorator | None:
    # First child is the attribute name
    name_node = attr.child_by_field_name("name")
    if name_node is None:
        if attr.named_children:
            name_node = attr.named_children[0]
        else:
            return None
    name = node_text(name_node, source).lstrip("\\")
    args: list[str] = []
    args_node = attr.child_by_field_name("parameters")
    if args_node is None:
        args_node = next((c for c in attr.named_children if c.type == "arguments"), None)
    if args_node is not None:
        for arg in args_node.named_children:
            if arg.type == "argument":
                args.append(node_text(arg, source))
            else:
                args.append(node_text(arg, source))
    return Decorator(name=name, args=args)


def extract_attributes(node: Node | list[Node] | None, source: bytes) -> list[Decorator]:
    """Extract PHP 8 attributes into structured Decorator models."""
    if node is None:
        return []
    nodes = node if isinstance(node, list) else [node]
    out: list[Decorator] = []
    for n in nodes:
        if n.type == "attribute_list":
            for grp in n.named_children:
                if grp.type == "attribute_group":
                    for attr in grp.named_children:
                        if attr.type == "attribute":
                            dec = _extract_decorator_from_attribute(attr, source)
                            if dec is not None:
                                out.append(dec)
                elif grp.type == "attribute":
                    dec = _extract_decorator_from_attribute(grp, source)
                    if dec is not None:
                        out.append(dec)
        elif n.type == "attribute_group":
            for attr in n.named_children:
                if attr.type == "attribute":
                    dec = _extract_decorator_from_attribute(attr, source)
                    if dec is not None:
                        out.append(dec)
        elif n.type == "attribute":
            dec = _extract_decorator_from_attribute(n, source)
            if dec is not None:
                out.append(dec)
        else:
            # Check if the node itself has an attribute_list child
            for child in n.named_children:
                if child.type == "attribute_list":
                    out.extend(extract_attributes(child, source))
    return out
