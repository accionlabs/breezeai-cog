"""Additive detector: Vue programmatic navigation is an outbound link, ``route`` /
``routeKind: navigation``::

    router.push('/reports')                 // router from useRouter()
    this.$router.replace({ path: '/x' })

A receiver counts only when the code shows what it is: bound from Vue Router's ``useRouter()``
— imported from ``'vue-router'``; Next.js has a hook of the same name — or ``$router``.
``{ path: '/x' }`` is the address; a named route (``{ name }``) is not.
Template links (``<router-link>``) are recorded by the template parser.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import node_text
from ..typescript.navigation import absolute, first_arg, literal, navigation, walk

_METHODS = frozenset({"push", "replace"})
_VUE_ROUTER = "vue-router"


def _hook_names(root: Node, source: bytes) -> set[str]:
    """The local names of Vue Router's ``useRouter`` (``import { useRouter } from 'vue-router'``,
    aliases included)."""
    names: set[str] = set()
    for stmt in root.named_children:
        src = stmt.child_by_field_name("source") if stmt.type == "import_statement" else None
        if src is None or node_text(src, source).strip("'\"") != _VUE_ROUTER:
            continue
        for spec in walk(stmt):
            name = spec.child_by_field_name("name") if spec.type == "import_specifier" else None
            if name is not None and node_text(name, source) == "useRouter":
                alias = spec.child_by_field_name("alias")
                names.add(node_text(alias if alias is not None else name, source))
    return names


def _bound_routers(source: bytes, hooks: set[str]) -> set[str]:
    """Variables bound from one of ``hooks`` (``const router = useRouter()``)."""
    if not hooks:
        return set()
    hook = b"|".join(re.escape(h.encode()) for h in sorted(hooks))
    bind = re.compile(rb"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:" + hook + rb")\s*\(\s*\)")
    return {m.group(1).decode() for m in bind.finditer(source)}


def _target(arg: Node, source: bytes) -> str | None:
    if arg.type == "object":  # { path: '/x' } — a named route ({ name }) is not an address
        for pair in arg.named_children:
            key = pair.child_by_field_name("key") if pair.type == "pair" else None
            if key is not None and node_text(key, source).strip("'\"") == "path":
                value = pair.child_by_field_name("value")
                return absolute(literal(value, source)) if value is not None else None
        return None
    return absolute(literal(arg, source))


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    routers = _bound_routers(source, _hook_names(dc.root, source)) \
        if _VUE_ROUTER.encode() in source else set()
    if not routers and b"$router" not in source:
        return None
    found: list[Emit] = []
    for node in walk(dc.root):
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        arg = first_arg(node)
        if fn is None or arg is None or fn.type != "member_expression":
            continue
        obj = fn.child_by_field_name("object")
        prop = fn.child_by_field_name("property")
        if obj is None or prop is None:
            continue
        recv = node_text(obj, source)
        if node_text(prop, source) in _METHODS and (recv in routers or recv.endswith("$router")):
            found.append(navigation(node, "vue", _target(arg, source), enrich=True))
    return Detection(found, label="vue") if found else None


register_detector(Detector(
    name="vue-navigation",
    # No single byte guard: useRouter() or $router, checked inside.
    gate=Gate("typescript", order=70, skip_fixtures=True),
    detect=_detect, frameworks=("vue",),
))
