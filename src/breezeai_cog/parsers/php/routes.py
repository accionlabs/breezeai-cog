"""PHP route argument helpers shared by PHP framework parsers."""

from __future__ import annotations

import re

from tree_sitter import Node

from ..treesitter import node_text


def extract_verbs(methods_node: Node | None, source: bytes) -> list[str]:
    """Extract HTTP verb strings from a PHP route methods argument."""
    if methods_node is None:
        return []

    curr = methods_node
    if curr.type == "argument" and curr.named_children:
        curr = curr.named_children[0]

    def get_string(node: Node) -> str | None:
        if node.type == "string":
            frag = next((c for c in node.named_children if c.type == "string_content"), None)
            if frag is not None:
                return node_text(frag, source)
            text = node_text(node, source)
            return text.strip("'\"")
        if node.type == "encapsed_string":
            return node_text(node, source).strip("'\"")
        return None

    single = get_string(curr)
    if single is not None:
        value = single.strip()
        return [value.upper()] if value else []

    verbs: list[str] = []
    for elem in curr.named_children:
        target: Node = elem
        if elem.type == "array_element_initializer":
            value_node = elem.child_by_field_name("value")
            if value_node is not None:
                target = value_node
            elif elem.named_children:
                target = elem.named_children[-1]
        verb_value: str | None = get_string(target)
        if verb_value and verb_value.strip():
            verbs.append(verb_value.strip().upper())
    return verbs


_HANDLER_STRING_RE = re.compile(r"^[\w\\]+(?:@|::)\w+$")
_METHOD_RE = re.compile(r"^\w+$")


def resolve_handler(arg_node: Node | None, source: bytes) -> str | None:
    r"""Resolve a PHP route handler to canonical ``Controller@method`` form."""
    if arg_node is None:
        return None

    curr = arg_node
    if curr.type == "argument" and curr.named_children:
        curr = curr.named_children[0]

    def get_string(node: Node) -> str | None:
        if node.type == "string":
            frag = next((c for c in node.named_children if c.type == "string_content"), None)
            if frag is not None:
                return node_text(frag, source)
            text = node_text(node, source)
            return text.strip("'\"")
        if node.type == "encapsed_string":
            return node_text(node, source).strip("'\"")
        return None

    value = get_string(curr)
    if value is not None:
        if _HANDLER_STRING_RE.match(value):
            return value.replace("::", "@", 1)
        return None

    if curr.type == "array_creation_expression":
        elements = curr.named_children
        if len(elements) == 2:
            def unwrap(element: Node) -> Node:
                if element.type == "array_element_initializer":
                    value_node = element.child_by_field_name("value")
                    if value_node is not None:
                        return value_node
                    if element.named_children:
                        return element.named_children[-1]
                return element

            class_node = unwrap(elements[0])
            method_node = unwrap(elements[1])
            if class_node.type == "class_constant_access_expression":
                class_text = node_text(class_node, source)
                if "::" in class_text:
                    class_name, constant = class_text.rsplit("::", 1)
                    method_name = get_string(method_node)
                    if constant.strip().lower() == "class" and method_name and _METHOD_RE.match(method_name):
                        return f"{class_name.strip()}@{method_name}"

    return None
