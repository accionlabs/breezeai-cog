"""Additive detector: Angular programmatic navigation is an outbound link, ``route`` /
``routeKind: navigation``::

    this.router.navigate(['/users'])        // a constructor parameter typed Router
    this.router.navigateByUrl('/users')

Angular resolves both from the root unless ``relativeTo`` is given, then the address depends
on the current route and is empty. A link array counts only when every segment is a literal.
Template links (``routerLink``) are recorded by the template parser.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import node_text
from ..typescript.navigation import first_arg, literal, navigation, walk

_METHODS = frozenset({"navigate", "navigateByUrl"})
_ROUTER_PARAM = re.compile(rb"\b(?:private|public|protected|readonly)\s+(?:readonly\s+)?(\w+)\s*:\s*Router\b")


def _target(method: str, call: Node, arg: Node, source: bytes) -> str | None:
    """``navigateByUrl('team/33')`` → ``/team/33``, ``navigate(['orders', 'new'])`` →
    ``/orders/new``; ``relativeTo`` → empty."""
    args = call.child_by_field_name("arguments")
    extras = args.named_children[1] if args is not None and len(args.named_children) > 1 else None
    if extras is not None and b"relativeTo" in node_text(extras, source).encode():
        return None
    if method == "navigateByUrl":
        url = literal(arg, source)
        return "/" + url.lstrip("/") if url is not None else None
    if arg.type != "array":  # navigate(['/users', id]) — every segment a literal
        return None
    parts = [literal(c, source) for c in arg.named_children]
    literals = [p for p in parts if p is not None]
    if not parts or len(literals) != len(parts):
        return None
    return "/" + "/".join(p.strip("/") for p in literals if p)


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    routers = {m.group(1).decode() for m in _ROUTER_PARAM.finditer(source)}
    if not routers:
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
        method = node_text(prop, source)
        if method in _METHODS and node_text(obj, source).removeprefix("this.") in routers:
            found.append(navigation(node, "angular", _target(method, node, arg, source),
                                    enrich=True))
    return Detection(found, label="angular") if found else None


register_detector(Detector(
    name="angular-navigation",
    gate=Gate("typescript", order=72, guard=b"@angular/router", skip_fixtures=True),
    detect=_detect, frameworks=("angular",),
))
