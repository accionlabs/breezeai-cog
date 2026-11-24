"""The markup a React component returns, written as JSX: its links, form posts and event props.

JSX is markup inside the code, so the TypeScript grammar already holds it. Each tag is handed to
the shared markup rules (:func:`..template_common.markup.tag_emit`) as its name and attributes,
so ``<a href>`` and ``<form action>`` give the same records as in any template. An event prop
(``onClick={save}``) gives a record named as written, with the function it calls as ``handler``.

A value written as code (``href={url}``) is kept as written and gives no endpoint; conditions
(``{admin && <button/>}``) and lists (``items.map(…)``) are not evaluated: each tag is read once,
as written. React Router's ``<Link>`` / ``<NavLink>`` are read by :mod:`.navigation`.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ..emits import Emit
from ..template_common.markup import tag_emit
from ..treesitter import node_text

_TAGS = ("jsx_opening_element", "jsx_self_closing_element")
#: An event prop: ``on`` and a capital (``onClick``, ``onSubmit``), as React names them.
_EVENT = re.compile(r"^on[A-Z]")
#: Tags whose links and form posts the shared markup rules read (plain HTML elements).
_MARKUP_TAGS = frozenset({"a", "area", "form"})


def jsx_emits(root: Node, source: bytes, *, links: bool = True) -> list[Emit]:
    """One emit per link (when ``links``), form post and event prop in the file's JSX."""
    if b"/>" not in source and b"</" not in source:
        return []  # cheap guard — no JSX in the file
    found: list[Emit] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _TAGS:
            found.extend(_tag(node, source, links))
        stack.extend(reversed(node.named_children))
    return found


def _tag(tag: Node, source: bytes, links: bool) -> list[Emit]:
    name = tag.child_by_field_name("name")
    tag_name = node_text(name, source) if name is not None else ""
    attrs: dict[str, str] = {}
    out: list[Emit] = []
    for attr in tag.named_children:
        if attr.type != "jsx_attribute" or not attr.named_children:
            continue
        key = node_text(attr.named_children[0], source)
        value = attr.named_children[1] if len(attr.named_children) > 1 else None
        attrs.setdefault(key.lower(), _value(value, source))
        if _EVENT.match(key):
            out.append(Emit(attr, {"name": key, "handler": _handler(value, source)}))
    if tag_name in _MARKUP_TAGS:
        emit = tag_emit(tag, tag_name, attrs, links)
        if emit is not None:
            out.insert(0, emit)
    return out


def _value(value: Node | None, source: bytes) -> str:
    """A prop's value as written: a string's contents, else the code with its braces (which the
    markup rules read as not literal)."""
    if value is None:
        return ""
    if value.type == "string":
        return node_text(value, source)[1:-1]
    return node_text(value, source)


def _handler(value: Node | None, source: bytes) -> str | None:
    """The function an event prop calls: ``{save}``, ``{this.save}``, ``{() => save(id)}``;
    None for anything else (an inline statement, a value built at run time)."""
    if value is None or value.type != "jsx_expression" or not value.named_children:
        return None
    expr = value.named_children[0]
    if expr.type == "arrow_function":
        body = expr.child_by_field_name("body")
        if body is None or body.type != "call_expression":
            return None
        expr = body.child_by_field_name("function") or body
    if expr.type == "identifier":
        return node_text(expr, source)
    if expr.type == "member_expression":
        obj, prop = expr.child_by_field_name("object"), expr.child_by_field_name("property")
        if obj is not None and prop is not None and obj.type == "this":
            return node_text(prop, source)
        return node_text(expr, source)
    return None
