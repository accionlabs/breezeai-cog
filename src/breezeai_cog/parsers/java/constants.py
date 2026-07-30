"""Java ``static final String`` constant collection — the raw initializer tokens for
constant folding (see :mod:`..constfold`). Used to resolve symbolic endpoint/address
arguments (``registerHandler(ADDRESS_WEB, h)`` → its literal value).

Faithful: only ``final`` String fields with a foldable initializer are collected; a
non-final field has no compile-time value and is skipped."""

from __future__ import annotations

from tree_sitter import Node

from ..constfold import Token, resolve_tokens
from ..treesitter import node_text

_TYPE_DECLS = ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration")


def _init_tokens(node: Node, source: bytes) -> list[Token] | None:
    """A Java initializer expression → constant-fold tokens, or ``None`` if it is not a
    string literal / ``+`` concatenation of literals and constant references."""
    t = node.type
    if t == "string_literal":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return [("lit", node_text(frag, source) if frag is not None else node_text(node, source).strip('"'))]
    if t == "binary_expression":  # only string `+` concatenation folds
        parts: list[Token] = []
        for child in node.named_children:
            sub = _init_tokens(child, source)
            if sub is None:
                return None
            parts += sub
        return parts
    if t == "parenthesized_expression":
        inner = next(iter(node.named_children), None)
        return _init_tokens(inner, source) if inner is not None else None
    if t in ("identifier", "field_access"):  # ref to another constant (NAME / Class.FIELD)
        return [("ref", node_text(node, source))]
    return None


def fold_arg(node: Node, source: bytes, values: dict[str, str]) -> str | None:
    """Fold a call-argument expression (a literal, a constant identifier, ``Class.FIELD``, or
    a ``+`` concatenation of those) to its String value, or ``None`` if it has no
    compile-time value under ``values`` (honest-null — a runtime variable never resolves)."""
    tokens = _init_tokens(node, source)
    return resolve_tokens(tokens, values) if tokens is not None else None


def _final_string_declarators(cls: Node, source: bytes):
    """Yield ``(name, initializer_node)`` for each ``static final String`` field on ``cls``."""
    body = cls.child_by_field_name("body")
    if body is None:
        return
    for member in body.named_children:
        if member.type != "field_declaration":
            continue
        mods = next((c for c in member.named_children if c.type == "modifiers"), None)
        mod_words = node_text(mods, source).split() if mods is not None else []
        if "final" not in mod_words:  # faithful: only the language's compile-time constants
            continue
        type_node = member.child_by_field_name("type")
        if type_node is None or node_text(type_node, source) != "String":
            continue
        for decl in member.named_children:
            if decl.type != "variable_declarator":
                continue
            name_node = decl.child_by_field_name("name")
            value_node = decl.child_by_field_name("value")
            if name_node is not None and value_node is not None:
                yield node_text(name_node, source), value_node


def collect_constants(root: Node, source: bytes) -> dict[str, list[Token]]:
    """Raw ``static final String`` constants in this file → ``{name: tokens}`` keyed by both
    the simple name and ``ClassName.name`` (so both a bare and a qualified reference resolve).
    Values are unresolved token lists — fold with :func:`..constfold.resolve_all`."""
    out: dict[str, list[Token]] = {}

    def walk(node: Node, class_name: str | None) -> None:
        for child in node.named_children:
            if child.type in _TYPE_DECLS:
                nm = child.child_by_field_name("name")
                cname = node_text(nm, source) if nm is not None else class_name
                for field_name, value_node in _final_string_declarators(child, source):
                    tokens = _init_tokens(value_node, source)
                    if tokens is not None:
                        out[field_name] = tokens
                        if cname:
                            out[f"{cname}.{field_name}"] = tokens
                walk(child, cname)  # nested types
            else:
                walk(child, class_name)

    walk(root, None)
    return out
