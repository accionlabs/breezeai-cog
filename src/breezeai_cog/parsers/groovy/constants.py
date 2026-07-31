"""Groovy ``static final String`` constant collection — the raw initializer tokens for
constant folding (see :mod:`..constfold`), used to resolve symbolic endpoint/address
arguments. Mirrors the Java collector; Groovy differs only in that modifier keywords
(``final``) are unnamed children of the field declaration. Faithful: only ``final`` String
fields with a foldable initializer are collected."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ..constfold import Token, init_tokens, resolve_tokens
from ..treesitter import node_text

_TYPE_DECLS = ("class_declaration", "interface_declaration", "enum_declaration")


def fold_arg(node: Node, source: bytes, values: dict[str, str]) -> str | None:
    """Fold a call-argument expression to its String value, or ``None`` if it has no
    compile-time value under ``values`` (honest-null)."""
    tokens = init_tokens(node, source)
    return resolve_tokens(tokens, values) if tokens is not None else None


def _is_final_string(field: Node, source: bytes) -> bool:
    if not any(c.type == "final" for c in field.children):  # `final` is an unnamed keyword
        return False
    type_node = field.child_by_field_name("type")
    return type_node is not None and node_text(type_node, source) == "String"


def _final_string_declarators(cls: Node, source: bytes) -> Iterator[tuple[str, Node]]:
    """Yield ``(name, initializer_node)`` for each ``static final String`` field on ``cls``."""
    body = cls.child_by_field_name("body")
    if body is None:
        return
    for member in body.named_children:
        if member.type != "field_declaration" or not _is_final_string(member, source):
            continue
        for decl in member.named_children:
            if decl.type != "variable_declarator":
                continue
            name_node = decl.child_by_field_name("name")
            value_node = decl.child_by_field_name("value")
            if name_node is not None and value_node is not None:
                yield node_text(name_node, source), value_node


def collect_constants(root: Node, source: bytes) -> dict[str, list[Token]]:
    """Raw ``static final String`` constants in this file → ``{name: tokens}`` keyed by the
    simple name and ``ClassName.name``. Fold with :func:`..constfold.resolve_all`."""
    out: dict[str, list[Token]] = {}

    def walk(node: Node, class_name: str | None) -> None:
        for child in node.named_children:
            if child.type in _TYPE_DECLS:
                nm = child.child_by_field_name("name")
                cname = node_text(nm, source) if nm is not None else class_name
                for field_name, value_node in _final_string_declarators(child, source):
                    tokens = init_tokens(value_node, source)
                    if tokens is not None:
                        out[field_name] = tokens
                        if cname:
                            out[f"{cname}.{field_name}"] = tokens
                walk(child, cname)
            else:
                walk(child, class_name)

    walk(root, None)
    return out
