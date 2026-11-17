"""PHP class-like declaration extraction."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, disambiguate
from ...schemas import Class, ConstructorParam
from ...schemas.enums import ClassType
from ..treesitter import line_span, node_text
from .functions import extract_attributes, extract_params

_CLASS_TYPES: dict[str, ClassType] = {
    "class_declaration": "class",
    "interface_declaration": "interface",
    "trait_declaration": "trait",
    "enum_declaration": "enum",
}


def build_class(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    seen_ids: set[str],
) -> Class:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else "<anonymous>"
    start, end = line_span(node)
    cid = disambiguate(class_id(path, name), seen_ids)
    extends: str | None = None
    implements: list[str] = []
    for child in node.named_children:
        if child.type == "base_clause":
            base = next(iter(child.named_children), None)
            if base is not None:
                extends = node_text(base, source)
        elif child.type in {"class_interface_clause", "base_list"}:
            implements.extend(node_text(base, source) for base in child.named_children)
    visibility = next(
        (node_text(child, source) for child in node.children
         if child.type in {"public", "protected", "private"}),
        None,
    )
    is_abstract = any(child.type == "abstract_modifier" for child in node.children)
    constructor_params: list[ConstructorParam] = []
    body = node.child_by_field_name("body")
    if body is not None:
        for member in body.named_children:
            if member.type != "method_declaration":
                continue
            method_name = member.child_by_field_name("name")
            if method_name is None or node_text(method_name, source).lower() != "__construct":
                continue
            constructor_params = [
                ConstructorParam(name=param.name, type=param.type)
                for param in extract_params(member.child_by_field_name("parameters"), source)
            ]
            break
    return Class.model_validate({
        "id": cid, "parentId": parent_id, "path": path, "name": name,
        "type": _CLASS_TYPES[node.type], "visibility": visibility,
        "isAbstract": is_abstract or None, "extends": extends,
        "implements": implements, "constructorParams": constructor_params,
        "decorators": extract_attributes(node, source),
        "startLine": start, "endLine": end,
    })

