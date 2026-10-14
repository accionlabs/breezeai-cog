"""Additive detector: programmatic navigation and React ``<Link>`` are outbound links,
``route`` / ``routeKind: navigation``.

A link or a router call moves the user to a page: the UI's outbound call, matched to a page
(``route`` / ``routeKind: page``) on ``endpoint``, as ``api_call`` is to ``route``::

    router.push('/reports')                 // Vue — router from useRouter(), or this.$router
    this.router.navigate(['/users'])        // Angular — a constructor parameter typed Router
    this.router.navigateByUrl('/users')
    navigate('/reports')                    // React — navigate from useNavigate()
    <Link to="/orders"> / <NavLink to=…>    // React Router

A receiver counts only when the code shows what it is (bound from ``useRouter()`` /
``useNavigate()``, typed ``Router``, ``$router``); ``Link`` only when imported from React Router.
The address is resolved or empty: a literal absolute path (Angular resolves from the root unless
``relativeTo`` is given; a link array of literals is joined); a relative path depends on the
current route, a variable or a named route is not an address — all empty (contract §6, §8). A
call that is its own statement enriches it; one nested in a larger statement gets its own record. Template-markup links (Vue ``<router-link>``, Angular
``routerLink``) are recorded by the template parsers.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import node_text

_VUE_METHODS = frozenset({"push", "replace"})
_ANGULAR_METHODS = frozenset({"navigate", "navigateByUrl"})
_LINK_TAGS = frozenset({"Link", "NavLink"})
_LINK_IMPORT = re.compile(rb"import\s*\{([^}]*)\}\s*from\s*['\"]react-router(?:-dom)?['\"]")
_HOOK_BIND = re.compile(rb"\b(?:const|let|var)\s+(\w+)\s*=\s*(useRouter|useNavigate)\s*\(\s*\)")
_ROUTER_PARAM = re.compile(rb"\b(?:private|public|protected|readonly)\s+(?:readonly\s+)?(\w+)\s*:\s*Router\b")


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for c in node.named_children:
        yield from _walk(c)


def _string(node: Node, source: bytes) -> str | None:
    if node.type == "string":
        return node_text(node, source)[1:-1]
    if node.type == "template_string" and not any(
        c.type == "template_substitution" for c in node.named_children
    ):
        return node_text(node, source)[1:-1]
    return None


def _absolute(path: str | None) -> str | None:
    return path if path is not None and path.startswith("/") else None


def _vue_target(arg: Node, source: bytes) -> str | None:
    if arg.type == "object":  # { path: '/x' } — a named route ({ name }) is not an address
        for pair in arg.named_children:
            key = pair.child_by_field_name("key") if pair.type == "pair" else None
            if key is not None and node_text(key, source).strip("'\"") == "path":
                value = pair.child_by_field_name("value")
                return _absolute(_string(value, source)) if value is not None else None
        return None
    return _absolute(_string(arg, source))


def _angular_target(method: str, call: Node, arg: Node, source: bytes) -> str | None:
    """Angular resolves both from the root unless ``relativeTo`` is given (then the address
    depends on the current route → empty): ``navigateByUrl('team/33')`` → ``/team/33``,
    ``navigate(['orders', 'new'])`` → ``/orders/new``."""
    args = call.child_by_field_name("arguments")
    extras = args.named_children[1] if args is not None and len(args.named_children) > 1 else None
    if extras is not None and b"relativeTo" in node_text(extras, source).encode():
        return None
    if method == "navigateByUrl":
        url = _string(arg, source)
        return "/" + url.lstrip("/") if url is not None else None
    if arg.type != "array":  # navigate(['/users', id]) — every segment a literal
        return None
    parts = [_string(c, source) for c in arg.named_children]
    literal = [p for p in parts if p is not None]
    if not parts or len(literal) != len(parts):
        return None
    return "/" + "/".join(p.strip("/") for p in literal if p)


def _first_arg(call: Node) -> Node | None:
    args = call.child_by_field_name("arguments")
    return args.named_children[0] if args is not None and args.named_children else None


#: Cheap pre-check: a file with none of these cannot navigate in a way this detector reads.
_MARKERS = (b"useRouter", b"$router", b"@angular/router", b"useNavigate", b"react-router")


def _link_names(source: bytes) -> set[str]:
    """Local names of ``Link`` / ``NavLink`` imported from React Router (``Link as L`` → ``L``)."""
    out: set[str] = set()
    for m in _LINK_IMPORT.finditer(source):
        for spec in m.group(1).decode().split(","):
            original, _, alias = spec.strip().partition(" as ")
            if original.strip() in _LINK_TAGS:
                out.add((alias or original).strip())
    return out


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    if not any(m in source for m in _MARKERS):
        return None
    hooks = {m.group(1).decode(): m.group(2).decode() for m in _HOOK_BIND.finditer(source)}
    vue_routers = {n for n, h in hooks.items() if h == "useRouter"}
    navigates = {n for n, h in hooks.items() if h == "useNavigate"}
    angular = {m.group(1).decode() for m in _ROUTER_PARAM.finditer(source)} \
        if b"@angular/router" in source else set()
    links = _link_names(source)

    found: list[Emit] = []
    frameworks: list[str] = []

    def add(node: Node, framework: str, endpoint: str | None, *, enrich: bool) -> None:
        found.append(Emit(
            node, {"semanticType": "route", "routeKind": "navigation", "framework": framework,
             "endpoint": endpoint},
            enrich="own" if enrich else "never",
        ))
        frameworks.append(framework)

    for node in _walk(dc.root):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            arg = _first_arg(node)
            if fn is None or arg is None:
                continue
            if fn.type == "identifier" and node_text(fn, source) in navigates:
                add(node, "react", _absolute(_string(arg, source)), enrich=True)
            elif fn.type == "member_expression":
                obj = fn.child_by_field_name("object")
                prop = fn.child_by_field_name("property")
                if obj is None or prop is None:
                    continue
                recv, method = node_text(obj, source), node_text(prop, source)
                if method in _VUE_METHODS and (recv in vue_routers or recv.endswith("$router")):
                    add(node, "vue", _vue_target(arg, source), enrich=True)
                elif method in _ANGULAR_METHODS and recv.removeprefix("this.") in angular:
                    add(node, "angular", _angular_target(method, node, arg, source), enrich=True)
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
                    target = _absolute(_string(value, source)) if value is not None else None
                    add(attr, "react", target, enrich=False)
    if not found:
        return None
    return Detection(found, label=frameworks[0])


register_detector(Detector(
    name="navigation",
    # No single byte guard: Vue / Angular / React markers are checked inside.
    gate=Gate("typescript", order=70, skip_fixtures=True),
    detect=_detect, frameworks=("vue", "angular", "react"),
))
