"""Additive detector: React programmatic navigation and ``<Link>`` are outbound links,
``route`` / ``routeKind: navigation``::

    navigate('/reports')                    // navigate from useNavigate()
    <Link to="/orders"> / <NavLink to=…>    // React Router

``navigate`` counts only when bound from ``useNavigate()``; ``Link`` only when imported from
React Router (``Link as L`` → ``L``).
"""

from __future__ import annotations

import re

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import node_text
from ..typescript.navigation import absolute, first_arg, literal, navigation, walk

_LINK_TAGS = frozenset({"Link", "NavLink"})
_LINK_IMPORT = re.compile(rb"import\s*\{([^}]*)\}\s*from\s*['\"]react-router(?:-dom)?['\"]")
_HOOK_BIND = re.compile(rb"\b(?:const|let|var)\s+(\w+)\s*=\s*useNavigate\s*\(\s*\)")


def _link_names(source: bytes) -> set[str]:
    """Local names of ``Link`` / ``NavLink`` imported from React Router."""
    out: set[str] = set()
    for m in _LINK_IMPORT.finditer(source):
        for spec in m.group(1).decode().split(","):
            original, _, alias = spec.strip().partition(" as ")
            if original.strip() in _LINK_TAGS:
                out.add((alias or original).strip())
    return out


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    navigates = {m.group(1).decode() for m in _HOOK_BIND.finditer(source)}
    links = _link_names(source)
    if not navigates and not links:
        return None
    found: list[Emit] = []
    for node in walk(dc.root):
        if node.type == "call_expression" and navigates:
            fn = node.child_by_field_name("function")
            arg = first_arg(node)
            if (fn is not None and arg is not None and fn.type == "identifier"
                    and node_text(fn, source) in navigates):
                found.append(navigation(node, "react", absolute(literal(arg, source)), enrich=True))
        elif node.type in ("jsx_opening_element", "jsx_self_closing_element") and links:
            name = node.child_by_field_name("name")
            if name is None or node_text(name, source) not in links:
                continue
            for attr in node.named_children:
                kids = attr.named_children if attr.type == "jsx_attribute" else []
                if kids and node_text(kids[0], source) == "to":
                    value = kids[1] if len(kids) > 1 else None
                    if value is not None and value.type == "jsx_expression" and value.named_children:
                        value = value.named_children[0]
                    target = absolute(literal(value, source)) if value is not None else None
                    found.append(navigation(attr, "react", target, enrich=False))
    return Detection(found, label="react") if found else None


register_detector(Detector(
    name="react-navigation",
    # No single byte guard: useNavigate() or a React Router import, checked inside.
    gate=Gate("typescript", order=74, skip_fixtures=True),
    detect=_detect, frameworks=("react",),
))
