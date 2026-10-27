"""Apex modifiers and annotations.

Every keyword sits in its own ``modifier`` node (``modifiers > modifier > public``), and
annotations are always ``annotation`` nodes whose arguments are ``key=value`` pairs.
Keywords and annotation names are case-insensitive in Apex, so names are compared in
lower case while the source spelling is kept in the emitted records.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Decorator
from ..treesitter import node_text

_VISIBILITY = ("public", "private", "protected", "global")


def declared_name(node: Node, source: bytes) -> str:
    name = node.child_by_field_name("name")
    return node_text(name, source) if name is not None else ""


def modifiers_node(node: Node) -> Node | None:
    return next((c for c in node.named_children if c.type == "modifiers"), None)


def modifier_keywords(modifiers: Node | None) -> set[str]:
    """Keyword node types in a ``modifiers`` node (``public``, ``static``, ``with_sharing`` …)."""
    if modifiers is None:
        return set()
    return {
        child.children[0].type
        for child in modifiers.named_children
        if child.type == "modifier" and child.children
    }


def visibility_of(keywords: set[str], default: str = "private") -> str:
    return next((v for v in _VISIBILITY if v in keywords), default)


def sharing_of(keywords: set[str]) -> str | None:
    for keyword, value in (
        ("with_sharing", "with"),
        ("without_sharing", "without"),
        ("inherited_sharing", "inherited"),
    ):
        if keyword in keywords:
            return value
    return None


def _argument(node: Node, source: bytes) -> str:
    """Raw argument text, as the spec's decorator rule asks: a bare string literal is
    unquoted (``'/x'`` → ``/x``), a ``key=value`` pair is kept as written (``urlMapping='/x'``)."""
    if node.type == "string_literal":
        return node_text(node, source).strip("'")
    return node_text(node, source)


def extract_annotations(modifiers: Node | None, source: bytes) -> list[Decorator]:
    if modifiers is None:
        return []
    out: list[Decorator] = []
    for node in modifiers.named_children:
        if node.type != "annotation":
            continue
        name = node.child_by_field_name("name")
        if name is None:
            continue
        arglist = node.child_by_field_name("arguments")
        args = [_argument(a, source) for a in arglist.named_children] if arglist is not None else []
        out.append(Decorator(name=node_text(name, source), args=args))
    return out


def has_annotation(decorators: list[Decorator], name: str) -> bool:
    return any(d.name.lower() == name.lower() for d in decorators)
