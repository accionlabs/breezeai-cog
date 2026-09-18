"""AST-backed Ruby framework claim helpers."""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text


def _nodes(root: Node):
    yield root
    for child in root.named_children:
        yield from _nodes(child)


def has_require(root: Node, source: bytes, required: set[str]) -> bool:
    for node in _nodes(root):
        if node.type != "call" or node.child_by_field_name("receiver") is not None:
            continue
        method = node.child_by_field_name("method")
        if method is None or node_text(method, source) != "require":
            continue
        arguments = node.child_by_field_name("arguments")
        first = arguments.named_children[0] if arguments is not None and arguments.named_children else None
        if first is not None and first.type == "string" and node_text(first, source).strip('"\'') in required:
            return True
    return False


def has_superclass(root: Node, source: bytes, required: set[str]) -> bool:
    return any(
        node.type == "class"
        and (superclass := node.child_by_field_name("superclass")) is not None
        and node_text(superclass, source) in required
        for node in _nodes(root)
    )


def has_call(root: Node, source: bytes, receiver_text: str, method_text: str) -> bool:
    return any(
        node.type == "call"
        and (method := node.child_by_field_name("method")) is not None
        and node_text(method, source) == method_text
        and (receiver := node.child_by_field_name("receiver")) is not None
        and node_text(receiver, source) == receiver_text
        for node in _nodes(root)
    )