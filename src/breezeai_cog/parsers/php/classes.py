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
    properties: list[dict[str, object]] = []
    if body is not None:
        for member in body.named_children:
            if member.type == "property_declaration":
                prop_visibility = next(
                    (node_text(child, source) for child in member.children
                     if child.type == "visibility_modifier" or child.type in {"public", "protected", "private"}),
                    None,
                )
                is_static = any(child.type == "static_modifier" or child.type == "static" for child in member.children)
                is_readonly = any(child.type == "readonly_modifier" or child.type == "readonly" for child in member.children)
                type_node = member.child_by_field_name("type")
                if type_node is None:
                    type_node = next(
                        (c for c in member.named_children
                         if c.type in {"named_type", "primitive_type", "optional_type", "union_type", "intersection_type"}),
                        None,
                    )
                type_str = node_text(type_node, source) if type_node is not None else None

                for elem in member.named_children:
                    if elem.type != "property_element":
                        continue
                    var_node = elem.child_by_field_name("name")
                    if var_node is None:
                        var_node = next((c for c in elem.named_children if c.type == "variable_name"), None)
                    var_name = node_text(var_node, source).lstrip("$") if var_node is not None else ""
                    default_node = elem.child_by_field_name("default_value")
                    if default_node is None:
                        default_node = next((c for c in elem.named_children if c != var_node), None)
                    default_str = node_text(default_node, source) if default_node is not None else None

                    properties.append({
                        "name": var_name,
                        "type": type_str,
                        "visibility": prop_visibility,
                        "isStatic": is_static,
                        "isReadonly": is_readonly,
                        "isConst": False,
                        "default": default_str,
                        "startLine": member.start_point.row + 1,
                        "endLine": member.end_point.row + 1,
                    })
            elif member.type == "const_declaration":
                const_visibility = next(
                    (node_text(child, source) for child in member.children
                     if child.type == "visibility_modifier" or child.type in {"public", "protected", "private"}),
                    "public",
                )
                for elem in member.named_children:
                    if elem.type != "const_element":
                        continue
                    cname_node = elem.child_by_field_name("name")
                    if cname_node is None:
                        cname_node = next((c for c in elem.named_children if c.type == "name"), None)
                    cname_str = node_text(cname_node, source) if cname_node is not None else ""
                    cval_node = elem.child_by_field_name("value")
                    if cval_node is None:
                        cval_node = next((c for c in elem.named_children if c != cname_node), None)
                    cval_str = node_text(cval_node, source) if cval_node is not None else None

                    properties.append({
                        "name": cname_str,
                        "type": None,
                        "visibility": const_visibility,
                        "isStatic": True,
                        "isReadonly": True,
                        "isConst": True,
                        "default": cval_str,
                        "startLine": member.start_point.row + 1,
                        "endLine": member.end_point.row + 1,
                    })

    return Class.model_validate({
        "id": cid, "parentId": parent_id, "path": path, "name": name,
        "type": _CLASS_TYPES[node.type], "visibility": visibility,
        "isAbstract": is_abstract or None, "extends": extends,
        "implements": implements, "constructorParams": constructor_params,
        "decorators": extract_attributes(node, source), "properties": properties,
        "startLine": start, "endLine": end,
    })

