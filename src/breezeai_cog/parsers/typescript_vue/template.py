"""Vue ``<template>`` block capture — closes the documented ``sfc.py`` gap.

``sfc.py`` blanks everything but ``<script>``, so template bindings (``@click``, ``v-for``,
``{{ }}``, ``<router-link>``) were never captured. Here we parse the SFC with the dedicated
``vue`` grammar (in ``tree_sitter_language_pack`` — no new dependency) and walk only the
``<template>`` block, emitting one flat ``Statement`` per binding / interpolation, mirroring the
Angular template capture. ``handler`` names the referenced method for an event binding, so a
component method joins to the template bindings that invoke it.

The ``vue`` grammar models every directive as one ``directive_attribute`` node (unlike
Angular's typed ``event_binding`` / ``property_binding`` nodes), so ``nodeType`` is
``directive_attribute`` uniformly — the raw grammar type, per the graph-boundary spec. The
binding *kind* rides on ``name`` (the directive argument/name) and ``handler`` (set only for
events). ``<router-link>`` navigation becomes a ``route`` statement (``routeKind="navigation"``),
reusing the Angular navigation model.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text

#: Vue Router / Nuxt link components whose ``to`` is page navigation (not an ordinary prop).
_NAV_TAGS = frozenset({"router-link", "RouterLink", "nuxt-link", "NuxtLink"})
#: Handler forms in an event value: a call ``remove(o)`` / ``svc.save()`` or a bare method ref
#: ``load``. An inline expression (``n++``, ``busy = true``) matches neither → honest-null.
_CALL_RE = re.compile(r"^\s*([A-Za-z_$][\w.$]*)\s*\(")
_IDENT_RE = re.compile(r"^\s*([A-Za-z_$][\w.$]*)\s*$")
#: First string literal in a ``:to`` value — the static base of the target route.
_LITERAL_RE = re.compile(r"""['"]([^'"]+)['"]""")


def _child(node: Node, kind: str) -> Node | None:
    return next((c for c in node.named_children if c.type == kind), None)


def _tag_name(attr_node: Node, source: bytes) -> str | None:
    """The tag owning this attribute — its ``start_tag``/``self_closing_tag`` parent's
    ``tag_name`` (used to tell a ``<router-link>`` ``to`` from an ordinary prop)."""
    parent = attr_node.parent
    tag = _child(parent, "tag_name") if parent is not None else None
    return node_text(tag, source) if tag is not None else None


def _attr_value(node: Node, source: bytes) -> str:
    """The quoted value's inner text (the bound expression), or ``""``."""
    q = _child(node, "quoted_attribute_value")
    val = _child(q, "attribute_value") if q is not None else _child(node, "attribute_value")
    return node_text(val, source) if val is not None else ""


def _directive(node: Node, source: bytes) -> tuple[str, bool]:
    """``(name, is_event)`` for a ``directive_attribute``. Event: ``@x`` / ``v-on:x`` → name is
    the event ``x``. Bind: ``:x`` / ``v-bind:x`` → name is the prop ``x``. Other directive:
    ``v-for`` / ``v-if`` / ``v-model`` → name is the directive minus ``v-`` (``for``/``if``/…)."""
    dname = _child(node, "directive_name")
    dname_txt = node_text(dname, source) if dname is not None else ""
    darg = _child(node, "directive_value")
    darg_txt = node_text(darg, source) if darg is not None else ""
    raw = node_text(node, source).lstrip()
    if dname_txt == "v-on" or raw.startswith("@"):
        return darg_txt, True
    if dname_txt == "v-bind" or (raw.startswith(":") and not raw.startswith("::")):
        return darg_txt, False
    if dname_txt.startswith("v-"):
        return dname_txt[2:], False  # v-for → for, v-model → model
    return darg_txt or dname_txt, False


def _handler(value_text: str) -> str | None:
    """The referenced method of an event value — ``load`` / ``remove`` / ``save``; honest-null
    for an inline expression."""
    m = _CALL_RE.match(value_text) or _IDENT_RE.match(value_text)
    return m.group(1).split(".")[-1] if m else None


def _nav_endpoint(value_text: str) -> str | None:
    """The target route of a ``router-link`` ``to`` — the first string literal, else None."""
    m = _LITERAL_RE.search(value_text)
    return m.group(1) if m else None


def _interpolation_name(node: Node, source: bytes) -> str:
    return node_text(node, source).strip().strip("{}").strip()


def collect_vue_template_statements(
    root: Node,
    source: bytes,
    path: str,
    parent_id: str,
    seen_ids: set[str],
    limit: int,
    *,
    framework: str = "vue",
    emit_routes: bool = True,
) -> list[Statement]:
    """Walk each ``<template>`` block and emit one flat Statement per binding / interpolation.
    ``<router-link>`` ``to`` becomes a ``route`` statement (``routeKind="navigation"``), gated by
    ``emit_routes`` (off for fixture files)."""
    out: list[Statement] = []
    stack: list[Node] = [c for c in root.named_children if c.type == "template_element"]
    while stack:
        node = stack.pop()
        start, col = node.start_point[0] + 1, node.start_point[1]
        text = node_text(node, source)
        clip = text if len(text) <= limit else text[:limit]

        def _emit(**fields: object) -> None:
            out.append(
                Statement(
                    id=disambiguate(statement_id(path, start, col), seen_ids),
                    parentId=parent_id,
                    text=clip,
                    startLine=start,
                    endLine=node.end_point[0] + 1,
                    path=path,
                    **fields,  # type: ignore[arg-type]
                )
            )

        if node.type == "interpolation":
            _emit(nodeType="interpolation", name=_interpolation_name(node, source))
            continue
        if node.type in ("directive_attribute", "attribute"):
            tag = _tag_name(node, source)
            if node.type == "directive_attribute":
                name, is_event = _directive(node, source)
            else:  # plain attribute — only interesting as a router-link `to`
                an = _child(node, "attribute_name")
                name, is_event = (node_text(an, source) if an is not None else ""), False
            value = _attr_value(node, source)
            if emit_routes and name == "to" and tag in _NAV_TAGS:
                _emit(
                    nodeType=node.type,
                    semanticType="route",
                    framework=framework,
                    routeKind="navigation",
                    endpoint=_nav_endpoint(value),
                    name="to",
                )
            elif node.type == "directive_attribute":
                _emit(
                    nodeType="directive_attribute",
                    name=name,
                    handler=_handler(value) if is_event else None,
                )
            continue  # plain non-nav attributes are skipped (no flood)
        stack.extend(reversed(node.named_children))
    return out
