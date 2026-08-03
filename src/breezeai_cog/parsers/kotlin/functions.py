from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text


_DECL_TYPES = frozenset({
    "class_declaration", "object_declaration",
    "function_declaration", "secondary_constructor",
})


def defined_names(root: Node, source: bytes) -> set[str]:
    """All top-level and nested declaration names (classes, objects, functions)."""
    names: set[str] = set()

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type in _DECL_TYPES:
                name_node = next(
                    (ch for ch in c.named_children
                     if ch.type in {"type_identifier", "simple_identifier"}),
                    None,
                )
                if name_node is not None:
                    names.add(node_text(name_node, source))
            walk(c)

    walk(root)
    return names


def type_map(root: Node, source: bytes) -> dict[str, str]:
    """Variable name → declared type, for receiver-type call resolution.

    Walks property_declaration and class_parameter nodes; class-level properties
    override local variables on name collision (DI pattern).
    """
    types: dict[str, str] = {}

    def add(name_node: Node | None, type_node: Node | None, *, override: bool) -> None:
        if name_node is None or type_node is None:
            return
        name = node_text(name_node, source)
        type_inner = next(
            (c for c in type_node.named_children if c.type == "type_identifier"),
            None,
        )
        type_text = node_text(type_inner, source) if type_inner is not None else node_text(type_node, source)
        if override or name not in types:
            types[name] = type_text

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "property_declaration":
                var_decl = next((ch for ch in c.named_children if ch.type == "variable_declaration"), None)
                if var_decl is not None:
                    ident = next((ch for ch in var_decl.named_children if ch.type == "simple_identifier"), None)
                    type_node = next(
                        (ch for ch in c.named_children if ch.type in {"user_type", "nullable_type"}),
                        None,
                    )
                    add(ident, type_node, override=True)
            elif c.type in {"class_parameter", "parameter"}:
                ident = next((ch for ch in c.named_children if ch.type == "simple_identifier"), None)
                type_node = next(
                    (ch for ch in c.named_children if ch.type in {"user_type", "nullable_type"}),
                    None,
                )
                add(ident, type_node, override=False)
            walk(c)

    walk(root)
    return types
