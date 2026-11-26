"""React Router route detection. Routes come in three shapes:

* **Declarative JSX** (v5/v6) — ``<Route path="users" element={<Users/>}>`` with nested
  ``<Route>`` children whose paths join onto the parent.
* **Data-router config objects** (v6) — ``createBrowserRouter([{ path, element, children }])``
  / ``useRoutes([...])``, the same ``{ path, ... }`` array shape Angular uses.
* **Framework-mode config DSL** (v7, ``@react-router/dev/routes``) — routes are built
  from ``route(path, file)`` / ``index(file)`` / ``layout(file, [children])`` /
  ``prefix(base, [children])`` **helper calls**, composed via ``const`` groups and
  ``...spread``. The ``route()/index()/layout()/prefix()`` call helpers exist only in v7,
  so this detection is gated on the ``@react-router/dev`` import and cannot fire on
  v5/v6 code (whose APIs are the JSX/object forms above).

Emits ``semanticType="route"`` statements with ``routeKind="page"`` — element routes and
``lazy`` code-split routes alike (a lazy route is a page whose component loads on demand, like
Angular ``loadComponent``). Routes are parented to the file (React routes are config/markup, not
handler methods), mirroring the Angular detector.

**Additive** (``react-routes``): a router file is not always owned by the React parser — one that
also imports a ``@nestjs/…`` DTO in a monorepo is the NestJS parser's — so this runs for every
TypeScript file that mentions ``react-router`` (``react-router-dom``, ``@react-router/dev``),
whatever parser owns it. A file that never mentions it has no routes, whatever its arrays look
like: a plain component's tab or menu list (``[{ path, component }]``) is data, and so is a Vue
router that holds the text ``'react'``. A route list split into a file without the import counts
when a React Router call takes it, through imports (:data:`ROUTE_MODULES_FACT`): passed in,
spread in, as a pathless layout's ``children``, or through a barrel that re-exports it. Only
the exported list taken counts, never the other lists in its file. A list
mounted as the ``children`` of a route with a path is not followed (its paths hang off a parent
that file doesn't know), and a wouter ``<Route>`` is not React Router (when unsure, record less).

Paths: a nested path that starts with ``/`` is absolute (React Router requires it to repeat its
parents' path), so it is not joined onto them. A constant path (``PATHS.home``, a `${…}` template
of consts) is folded through the constants this file sees (``typescript.scopes``). A ``path`` that is neither a literal
nor a constant that folds (``undefined``, ``'/a/' + id``) gives ``endpoint`` null, and so do the
relative paths under it — never the expression's text.

Wiring has no record (contract §4): a path-only parent (nested routes, no element), a layout (an
element whose children include an index or empty-path child, which always fills its outlet), a
pathless layout route, and v7 ``layout()`` / ``prefix()``. A layout whose element is a guard
(``<Route element={<ProtectedRoute/>}>``, with or without a path) puts its name in the children's
``guards``; an element counts as a guard only by whole words of its name (:func:`_is_guard_element`) — any other
layout is plain wiring (when unsure, record less)."""

from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...emit import file_id
from ..additive import (
    DetectContext,
    Detection,
    Detector,
    FactSpec,
    Gate,
    index_fact,
    register_detector,
    register_fact,
)
from ..emits import Emit
from ..navigation_common import literal
from ..treesitter import entry_text, node_text
from ..typescript.imports import (
    Consts,
    _module_of,
    _resolve,
    relative_requires,
    resolve_const_value,
)
from ..typescript.scopes import file_consts


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _child_path(prefix: str | None, sub: str | None) -> str | None:
    """A JSX / config route's full path under its parents' ``prefix``. An absolute ``sub``
    (``/p/kid`` under ``/p``) is already the full path. None when the path, or a parent's under a
    relative one, is not a literal (the address is unknown)."""
    if sub is not None and sub.startswith("/"):
        return _join("", sub)
    if prefix is None or sub is None:
        return None
    return _join(prefix, sub)


def _path_literal(node: Node | None, source: bytes,
                  const_values: Consts | None = None) -> str | None:
    """A JSX / config route's own ``path``: a string or a template without substitutions, also in
    JSX braces (``path={"/x"}``), or a constant this file sees folds to one
    (``PATHS.home``, a `${…}` template of consts). Anything else → None."""
    if node is not None and node.type == "jsx_expression":
        node = node.named_children[0] if node.named_children else None
    if node is None:
        return None
    value = literal(node, source)
    if value is None and const_values:
        value = resolve_const_value(node, source, const_values)
    return value


def _string_val(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else ""
    return node_text(node, source).strip("'\"`")


_JSX_ELEMENTS = ("jsx_element", "jsx_self_closing_element")
#: Redirect elements: a route that renders one is an alias, not a page — no record.
_REDIRECTS = frozenset({"Navigate", "Redirect"})


def _component_name(value: Node | None, source: bytes) -> str | None:
    """The page an ``element`` attr/prop renders -> its component name (``<Home/>`` -> ``Home``).

    Wrappers are not the page: an element whose only element child is another element is
    unwrapped to that child (``<Suspense fallback={…}><Dashboard/></Suspense>`` -> ``Dashboard``,
    ``<ErrorBoundary><Layout><Home/></Layout></ErrorBoundary>`` -> ``Home``). An element with
    more than one element child has no single page -> None (honest-null). A ``Component: Home``
    reference (data router) is the component itself."""
    if value is None:
        return None
    if value.type == "identifier":  # Component: Home
        return node_text(value, source)
    node: Node | None = value
    # element={<Home/>} / element: (<Home/>) — the JSX inside the braces or parentheses
    while node is not None and node.type in ("jsx_expression", "parenthesized_expression"):
        node = next((c for c in node.named_children
                     if c.type in (*_JSX_ELEMENTS, "parenthesized_expression")), None)
    while node is not None and node.type == "jsx_element":
        kids = [c for c in node.named_children if c.type in _JSX_ELEMENTS]
        if not kids:  # <Home>text</Home>
            break
        if len(kids) > 1:
            return None
        node = kids[0]
    if node is None:
        return None
    tag = node.child(0) if node.type == "jsx_element" else node  # opening element
    name = tag.child_by_field_name("name") if tag is not None else None
    return node_text(name, source) if name is not None else None


# ---- layouts and guard elements ------------------------------------------------

#: Words of a layout element's name that mark it as a guard (``ProtectedRoute``, ``PrivateOutlet``,
#: ``AuthGuard``); a name that also says it is a layout / page is not one.
_GUARD_WORDS = frozenset({"Protected", "Private", "Guard", "Authenticated", "Authorized"})
#: ``Require`` counts only before one of these (``RequireAuth``, ``RequireRole``), not in
#: ``RequiredFieldsNotice`` / ``RequireJsLoader``.
_REQUIRE_WHAT = frozenset({"Auth", "Login", "User", "Role", "Admin", "Permission", "Session"})
_NOT_GUARD_WORDS = frozenset({"Layout", "Page", "Shell"})
_NEGATIONS = frozenset({"Non", "Not", "No"})
_WORD_RE = re.compile(r"[A-Z][a-z0-9]*|[a-z0-9]+")


def _is_guard_element(name: str | None) -> bool:
    """Whether a layout element's name says it is a guard, by whole words of the name (split at
    capitals): ``ProtectedRoute``, ``AuthGuard``, ``RequireAuth`` — not ``UnprotectedRoute``,
    ``NonAuthorizedRedirect``, ``RequiredFieldsNotice``, nor a name that also says it is a layout
    / page."""
    if not name:
        return False
    words = _WORD_RE.findall(name.rsplit(".", 1)[-1])
    if _NOT_GUARD_WORDS.intersection(words):
        return False
    before = [None, *words[:-1]]  # a guard word after a negation (NonAuthorized…) is not one
    return any(w in _GUARD_WORDS and prev not in _NEGATIONS for prev, w in zip(before, words)) \
        or any(w == "Require" and nxt in _REQUIRE_WHAT for w, nxt in zip(words, words[1:]))


# ---- JSX <Route> form -------------------------------------------------------

def _opening(el: Node) -> Node:
    """The opening/self-closing tag of a jsx element (carries name + attributes)."""
    return el if el.type == "jsx_self_closing_element" else el.child(0)


def _is_route(el: Node, source: bytes) -> bool:
    if el.type not in ("jsx_element", "jsx_self_closing_element"):
        return False
    name = _opening(el).child_by_field_name("name")
    return name is not None and node_text(name, source) == "Route"


def _jsx_attrs(el: Node, source: bytes) -> dict[str, Node]:
    out: dict[str, Node] = {}
    for attr in _opening(el).named_children:
        if attr.type != "jsx_attribute":
            continue
        kids = attr.named_children
        if kids and kids[0].type == "property_identifier":
            out[node_text(kids[0], source)] = kids[1] if len(kids) > 1 else None
    return out


#: Attributes / keys through which a route shows a component of its own.
_ELEMENT_KEYS = ("element", "Component", "component", "render", "lazy")


def _fills_outlet(keys: Collection[str], path_value: Node | None, source: bytes,
                  const_values: Consts | None = None) -> bool:
    """An index route, or one with ``path=""`` (a constant that folds to it too) — it renders at
    its parent's URL."""
    return ("index" in keys and "path" not in keys) or (
        "path" in keys and _path_literal(path_value, source, const_values) == "")


def _process_jsx(el: Node, prefix: str | None, source: bytes, path: str,
                 routes: list[Emit], inherited: tuple[str, ...] = (),
                 const_values: Consts | None = None) -> None:
    attrs = _jsx_attrs(el, source)
    has_path = "path" in attrs
    is_index = "index" in attrs and not has_path  # <Route index .../> -> parent's path
    sub = _path_literal(attrs.get("path"), source, const_values) if has_path else ""
    full = _child_path(prefix, sub)
    handler = _component_name(attrs.get("element"), source)
    nested = [c for c in el.named_children if _is_route(c, source)] if el.type == "jsx_element" else []
    shows = any(k in attrs for k in _ELEMENT_KEYS)
    # A guard element around nested routes runs before each of them (it renders their outlet).
    guard_layout = bool(nested) and bool(handler) and _is_guard_element(handler)
    # Wiring: nested routes under a path with no element, a layout (a child fills the outlet),
    # or a guard layout.
    wiring = bool(nested) and (guard_layout or not shows or any(
        _fills_outlet(a, a.get("path"), source, const_values) for a in (_jsx_attrs(c, source) for c in nested)))
    if (has_path or is_index) and handler not in _REDIRECTS and not wiring:
        routes.append(Emit(el, dict(
            semanticType="route",
            endpoint=full,
            framework="react",
            routeKind="page",  # a lazy route is a page too, its component loaded on demand
            handler=handler,
            guards=list(inherited) or None,
        ), parent=file_id(path), node_type=el.type, text=entry_text(_opening(el), source)))
    child_guards = (*inherited, handler) if guard_layout and handler else inherited
    # Recurse into nested <Route> children (paired elements only), joining paths.
    for child in nested:
        _process_jsx(child, full if has_path else prefix, source, path, routes, child_guards,
                     const_values)


# ---- config-object form -----------------------------------------------------

def _key(pair: Node, source: bytes) -> str | None:
    k = pair.child_by_field_name("key")
    return node_text(k, source).strip("'\"") if k is not None else None


def _pairs(obj: Node, source: bytes) -> dict[str, Node]:
    out: dict[str, Node] = {}
    for c in obj.named_children:
        if c.type == "pair":
            key = _key(c, source)
            value = c.child_by_field_name("value")
            if key and value is not None:
                out[key] = value
    return out


def _keys(obj: Node, source: bytes) -> set[str]:
    """Every key an object sets: ``key: value`` pairs, methods (``loader() {}``, ``async lazy()
    {}``) and shorthand (``{ path, Component }``). A computed method name has no key. Only the
    pairs have a value to read (:func:`_pairs`)."""
    keys: set[str] = set()
    for c in obj.named_children:
        key: str | None = None
        if c.type == "pair":
            key = _key(c, source)
        elif c.type == "method_definition":
            name = c.child_by_field_name("name")
            if name is not None and name.type != "computed_property_name":
                key = node_text(name, source).strip("'\"")
        elif c.type == "shorthand_property_identifier":
            key = node_text(c, source)
        if key:
            keys.add(key)
    return keys


#: Keys only a React Router route object has. A ``path`` alone is not enough: breadcrumb, menu
#: and step lists (``{ path: '/home', label: 'Home' }``) have one too.
_ROUTER_KEYS = frozenset({
    "element", "Component", "component", "children", "lazy", "loader", "action",
    "errorElement", "ErrorBoundary", "index", "handle", "caseSensitive", "HydrateFallback",
    "shouldRevalidate",
})


def _is_route_array(arr: Node, source: bytes) -> bool:
    """An array of route objects: one has a ``path`` and a React Router key
    (:data:`_ROUTER_KEYS`), or is a pathless layout route (an element or component with
    ``children``) — the shape of a router whose top level is a layout."""
    for e in arr.named_children:
        if e.type != "object":
            continue
        keys = _keys(e, source)
        if ("path" in keys and keys & _ROUTER_KEYS) or (
                "children" in keys and keys & {"element", "Component"}):
            return True
    return False


def _is_children_value(arr: Node, source: bytes) -> bool:
    p = arr.parent
    return p is not None and p.type == "pair" and _key(p, source) == "children"


def _process_config(arr: Node, prefix: str | None, source: bytes, path: str,
                    routes: list[Emit], inherited: tuple[str, ...] = (),
                    const_values: Consts | None = None) -> None:
    for elem in arr.named_children:
        if elem.type != "object":
            continue
        pairs = _pairs(elem, source)
        keys = _keys(elem, source)
        is_index = "index" in keys and "path" not in keys  # index route -> parent's path
        children = pairs.get("children")
        nested = [c for c in children.named_children if c.type == "object"] \
            if children is not None and children.type == "array" else []
        element = pairs.get("element") or pairs.get("Component") or pairs.get("component")
        if element is not None and element.type == "identifier" and element is pairs.get("element"):
            element = None  # element: someVar — a value, not a component name
        handler = _component_name(element, source)
        if "path" not in keys and not is_index:
            # A pathless layout route: no record; a guard element guards its children.
            if children is not None and nested:
                guards = (*inherited, handler) if handler and _is_guard_element(handler) \
                    else inherited
                _process_config(children, prefix, source, path, routes, guards, const_values)
            continue
        # A shorthand ``{ path }`` has no value to read: an unknown path, like an unresolved
        # ``path: PATHS.x``.
        full = _child_path(prefix, "" if is_index
                           else _path_literal(pairs.get("path"), source, const_values))
        if handler in _REDIRECTS:
            continue
        shows = any(k in keys for k in _ELEMENT_KEYS)
        guard_layout = bool(nested) and bool(handler) and _is_guard_element(handler)
        wiring = bool(nested) and (guard_layout or not shows or any(
            _fills_outlet(_keys(c, source), _pairs(c, source).get("path"), source, const_values)
            for c in nested))
        if wiring and children is not None:
            guards = (*inherited, handler) if guard_layout and handler else inherited
            _process_config(children, full, source, path, routes, guards, const_values)
            continue
        routes.append(Emit(elem, dict(
            semanticType="route",
            endpoint=full,
            framework="react",
            routeKind="page",  # a lazy route is a page too, its component loaded on demand
            handler=handler,
            guards=list(inherited) or None,
        ), parent=file_id(path), text=entry_text(elem, source)))
        if children is not None and nested:
            _process_config(children, full, source, path, routes, inherited, const_values)


# ---- entry point ------------------------------------------------------------

def _walk(node: Node, typ: str, out: list[Node]) -> None:
    if node.type == typ:
        out.append(node)
    for c in node.named_children:
        _walk(c, typ, out)


def _has_route_ancestor(el: Node, source: bytes) -> bool:
    p = el.parent
    while p is not None:
        if _is_route(p, source):
            return True
        p = p.parent
    return False


# ---- v7 framework-mode config DSL (route/index/layout/prefix calls) ---------

_V7_HELPERS = frozenset({"route", "index", "layout", "prefix"})


def _callee_name(call: Node, source: bytes) -> str | None:
    """The called helper's bare name, or None if the callee isn't a plain identifier."""
    fn = call.child_by_field_name("function")
    return node_text(fn, source) if fn is not None and fn.type == "identifier" else None


def _call_args(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return list(args.named_children) if args is not None else []


def _last_array(args: list[Node]) -> Node | None:
    return args[-1] if args and args[-1].type == "array" else None


def _collect_consts(root: Node, source: bytes) -> dict[str, Node]:
    """Map ``const <name> = <expr>`` → value node, so ``...spread`` of a route group
    resolves to its definition (v7 composes groups across module-level consts)."""
    consts: dict[str, Node] = {}
    decls: list[Node] = []
    _walk(root, "variable_declarator", decls)
    for d in decls:
        name = d.child_by_field_name("name")
        value = d.child_by_field_name("value")
        if name is not None and name.type == "identifier" and value is not None:
            consts[node_text(name, source)] = value
    return consts


def _emit_v7(call: Node, kind: str, endpoint: str, handler_arg: Node | None,
             source: bytes, path: str, routes: list[Emit]) -> None:
    routes.append(Emit(call, dict(
        semanticType="route",
        endpoint=endpoint,
        framework="react",
        routeKind="page",
        handler=_string_val(handler_arg, source) or None,
    ), parent=file_id(path), node_type="call_expression",
        text=node_text(call, source).split("\n", 1)[0][:120]))


def _path_arg(node: Node | None, source: bytes, const_values: Consts | None) -> str:
    """A route/prefix path argument → its resolved string. Folds a constant (``paths.discover.root``
    / a `${…}` template of consts) via the constants this file sees (``typescript.scopes``); falls back to the raw text
    when it doesn't fully resolve — so an unresolved path is no worse than today, never a *different*
    wrong URL (honest-null)."""
    if node is not None and const_values:
        resolved = resolve_const_value(node, source, const_values)
        if resolved is not None:
            return resolved
    return _string_val(node, source)


def _process_v7(node: Node, prefix: str, source: bytes, path: str,
                routes: list[Emit], consts: dict[str, Node],
                guard: frozenset[str], const_values: Consts | None) -> None:
    """Recursively resolve a v7 route node under the accumulated ``prefix``. Handles the
    array / spread / const-identifier / helper-call forms; ``guard`` breaks const cycles.
    ``const_values`` folds a path built from a ``{…} as const`` object into its literal URL."""
    if node.type == "array":
        for child in node.named_children:
            _process_v7(child, prefix, source, path, routes, consts, guard, const_values)
        return
    if node.type == "spread_element":
        inner = node.named_children[0] if node.named_children else None
        if inner is not None:
            _process_v7(inner, prefix, source, path, routes, consts, guard, const_values)
        return
    if node.type == "identifier":  # ...groupConst -> resolve to its definition (once)
        name = node_text(node, source)
        target = consts.get(name)
        if target is not None and name not in guard:
            _process_v7(target, prefix, source, path, routes, consts, guard | {name}, const_values)
        return
    if node.type != "call_expression":
        return
    kind = _callee_name(node, source)
    if kind not in _V7_HELPERS:
        return
    args = _call_args(node)
    if kind == "route":  # route(path, file, opts?)
        seg = _path_arg(args[0], source, const_values) if args else ""
        handler = args[1] if len(args) > 1 else None
        _emit_v7(node, kind, _join(prefix, seg), handler, source, path, routes)
    elif kind == "index":  # index(file, opts?) -> renders at the parent path
        _emit_v7(node, kind, _join(prefix, ""), args[0] if args else None, source, path, routes)
    elif kind == "layout":  # layout(file, [children]) -> wiring: no record, no path segment
        children = _last_array(args)
        if children is not None:
            _process_v7(children, prefix, source, path, routes, consts, guard, const_values)
    elif kind == "prefix":  # prefix(base, [children]) -> add base, no node of its own
        base = _path_arg(args[0], source, const_values) if args else ""
        children = _last_array(args)
        if children is not None:
            _process_v7(children, _join(prefix, base), source, path, routes, consts, guard, const_values)


def _v7_entry(root: Node, source: bytes) -> Node | None:
    """The ``export default [...]`` route array (optionally wrapped in ``satisfies``)."""
    exports: list[Node] = []
    _walk(root, "export_statement", exports)
    for exp in exports:
        for child in exp.named_children:
            node = child
            if node.type == "satisfies_expression":  # [...] satisfies RouteConfig
                node = next((c for c in node.named_children if c.type == "array"), None)
            if node is not None and node.type == "array":
                return node
    return None


def _detect_v7(root: Node, source: bytes, path: str, routes: list[Emit],
               const_values: Consts | None) -> None:
    # Gate: the route/index/layout/prefix call helpers exist only in v7 framework mode.
    # Keying on the import keeps this inert on v5/v6 (react-router-dom) code.
    if b"@react-router/dev" not in source:
        return
    entry = _v7_entry(root, source)
    if entry is not None:
        _process_v7(entry, "", source, path, routes,
                    _collect_consts(root, source), frozenset(), const_values)


def detect_react_routes(root: Node, source: bytes, path: str, *,
                        const_values: Consts | None = None) -> list[Emit]:
    """This file's React Router page routes (JSX, config objects, v7 route modules), for
    :func:`~..emits.apply`."""
    routes: list[Emit] = []
    # JSX <Route>: start only at top-level Routes (no <Route> ancestor); recursion handles nesting.
    jsx: list[Node] = []
    _walk(root, "jsx_element", jsx)
    _walk(root, "jsx_self_closing_element", jsx)
    for el in jsx:
        if _is_route(el, source) and not _has_route_ancestor(el, source):
            _process_jsx(el, "", source, path, routes, const_values=const_values)
    # Config objects: route arrays that are not a nested ``children:`` value.
    arrays: list[Node] = []
    _walk(root, "array", arrays)
    for arr in arrays:
        if _is_route_array(arr, source) and not _is_children_value(arr, source):
            _process_config(arr, "", source, path, routes, const_values=const_values)
    # v7 framework-mode config DSL (gated on the @react-router/dev import).
    _detect_v7(root, source, path, routes, const_values)
    return routes


# Byte guards for a React file: a router import (routes) OR a bare ``react`` import — the
# latter is what a plain component file (``Button.tsx``) has. ``'react'`` / ``"react"`` match
# ``from 'react'`` exactly and do NOT match ``react-dom`` / ``react-router-dom`` (more chars
# precede the closing quote). The React parser's ``claims``; routes need the router itself.
REACT_GUARDS = (b"react-router", b"'react'", b'"react"')


# ---- route modules: a route list split into a file without the import ----------

#: The React Router calls that take a route list.
_ROUTER_CALLS = frozenset({
    "createBrowserRouter", "createHashRouter", "createMemoryRouter", "createStaticRouter",
    "useRoutes",
})
#: ``react.route-modules``: ``{file: its route lists}`` — the lists (by local name, ``default``
#: for an unnamed default export) that reach a React Router call through imports.
ROUTE_MODULES_FACT = "react.route-modules"
_DEFAULT = "default"


def _list_refs(node: Node, source: bytes) -> set[str]:
    """Names a route list takes top-level routes from: the list itself
    (``createBrowserRouter(routes)``), a ``...spread`` in it, or the ``children`` of a pathless
    layout in it. ``children`` under a route with a path are not followed: their paths hang off a
    parent the other file doesn't know (when unsure, record less)."""
    if node.type == "identifier":
        return {node_text(node, source)}
    refs: set[str] = set()
    if node.type != "array":
        return refs  # createBrowserRouter(buildRoutes()): not a list to follow
    for e in node.named_children:
        if e.type == "spread_element" and e.named_children \
                and e.named_children[0].type == "identifier":
            refs.add(node_text(e.named_children[0], source))
        elif e.type == "object" and "path" not in _keys(e, source):
            children = _pairs(e, source).get("children")
            if children is not None:
                refs |= _list_refs(children, source)
    return refs


def _import_bindings(root: Node, source: bytes) -> dict[str, tuple[str, str]]:
    """Local name → (module, the name it is exported under there): ``import r from`` and
    ``const r = require(…)`` are ``default``, ``import { a as b }`` is ``a``."""
    out: dict[str, tuple[str, str]] = {}
    for node in root.named_children:
        if node.type != "import_statement" or (module := _module_of(node, source)) is None:
            continue
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        for c in clause.named_children if clause is not None else ():
            if c.type == "identifier":
                out[node_text(c, source)] = (module, _DEFAULT)
            elif c.type == "named_imports":
                for spec in c.named_children:
                    name, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                    if spec.type == "import_specifier" and name is not None:
                        out[node_text(alias or name, source)] = (module, node_text(name, source))
    for local, binding in relative_requires(root, source).items():
        out.setdefault(local, binding)
    return out


def _list_value(node: Node | None) -> Node | None:
    """An array, or a name, under ``as`` / ``satisfies`` / parentheses; else None."""
    while node is not None and node.type in ("as_expression", "satisfies_expression",
                                             "parenthesized_expression"):
        node = node.named_children[0] if node.named_children else None
    return node if node is not None and node.type in ("array", "identifier") else None


def _file_lists(root: Node, source: bytes) -> tuple[dict[str, Node], dict[str, str]]:
    """A file's top-level lists (local name, or ``default`` for an unnamed default export → the
    array) and its exports (exported name → local name)."""
    lists: dict[str, Node] = {}
    exports: dict[str, str] = {}

    def declare(decl: Node, exported: bool) -> None:
        for d in decl.named_children:
            name = d.child_by_field_name("name") if d.type == "variable_declarator" else None
            value = _list_value(d.child_by_field_name("value")) if name is not None else None
            if name is None or name.type != "identifier":
                continue
            if value is not None and value.type == "array":
                lists[node_text(name, source)] = value
            if exported:
                exports[node_text(name, source)] = node_text(name, source)

    def default(value: Node | None) -> None:
        value = _list_value(value)
        if value is not None and value.type == "array":
            lists[_DEFAULT] = value
            exports[_DEFAULT] = _DEFAULT
        elif value is not None:
            exports[_DEFAULT] = node_text(value, source)

    for node in root.named_children:
        if node.type in ("lexical_declaration", "variable_declaration"):
            declare(node, False)
        elif node.type == "export_statement" and node.child_by_field_name("source") is None:
            decl = node.child_by_field_name("declaration")
            if decl is not None and decl.type in ("lexical_declaration", "variable_declaration"):
                declare(decl, True)
            elif node.child_by_field_name("value") is not None:
                default(node.child_by_field_name("value"))
            for clause in (c for c in node.named_children if c.type == "export_clause"):
                for spec in clause.named_children:
                    name, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
                    if name is not None:
                        exports[node_text(alias or name, source)] = node_text(name, source)
        elif node.type == "expression_statement" and node.named_children \
                and node.named_children[0].type == "assignment_expression":
            assign = node.named_children[0]
            left = assign.child_by_field_name("left")
            if left is not None and node_text(left, source) == "module.exports":
                default(assign.child_by_field_name("right"))
    return lists, exports


def _collect_route_modules(root: Node, source: bytes, rel: str, repo_root: Path | None) -> Any:
    """One file's part of :data:`ROUTE_MODULES_FACT`: the lists it passes to a React Router call
    (only in a file that mentions ``react-router``), and — followed only once a list is reached —
    what its exports name, which lists each of its lists takes routes from, and its re-exports."""
    if repo_root is None:
        return None
    imports = _import_bindings(root, source)
    lists, exports = _file_lists(root, source)
    router: set[tuple[str, str]] = set()  # (module, exported name) or ("", local list)
    if b"react-router" in source:
        calls: list[Node] = []
        _walk(root, "call_expression", calls)
        for call in calls:
            args = _call_args(call)
            if _callee_name(call, source) in _ROUTER_CALLS and args:
                for n in _list_refs(args[0], source):
                    if n in lists:
                        router.add(("", n))
                    elif n in imports:
                        router.add(imports[n])
    refs = {name: (sorted(n for n in _list_refs(arr, source) if n in lists and n != name),
                   sorted(imports[n] for n in _list_refs(arr, source)
                          if n in imports and n not in lists))
            for name, arr in lists.items()}
    reexports: dict[str, tuple[str, str]] = {}
    stars: list[str] = []
    for node in root.named_children:
        if node.type != "export_statement" or node.child_by_field_name("source") is None:
            continue
        module = _module_of(node, source)
        clause = next((c for c in node.named_children if c.type == "export_clause"), None)
        if module is None:
            continue
        if clause is None:
            stars.append(module)  # export * from './m'
            continue
        for spec in clause.named_children:
            name, alias = spec.child_by_field_name("name"), spec.child_by_field_name("alias")
            if name is not None:
                reexports[node_text(alias or name, source)] = (module, node_text(name, source))
    if not (router or refs or reexports or stars):
        return None
    return (rel, str(repo_root), sorted(router), sorted(exports.items()),
            sorted(refs.items()), sorted(reexports.items()), stars)


def _resolve_route_modules(values: list[Any], index: Any) -> dict[str, frozenset[str]] | None:
    """The lists a React Router call takes routes from, through any chain of imports, spreads,
    pathless layouts and re-exports: only the exported names asked for, never a file's other
    lists. ``export *`` passes on every name but ``default``."""
    files = {v[0]: v for v in values}
    found: dict[str, set[str]] = {}
    todo: list[tuple[str, bool, str]] = []  # (file, is a local list, name)

    def follow(rel: str, root: str, module: str, name: str) -> None:
        if (f := _resolve(module, rel, Path(root), index)) is not None:
            todo.append((f, False, name))

    for rel, root, router, *_ in values:
        for module, name in router:
            if module:
                follow(rel, root, module, name)
            else:
                todo.append((rel, True, name))
    seen: set[tuple[str, bool, str]] = set()
    while todo:
        item = todo.pop()
        if item in seen or item[0] not in files:
            continue
        seen.add(item)
        f, local, name = item
        rel, root, _, exports, refs, reexports, stars = files[f]
        if local:
            own = dict(refs).get(name)
            if own is None:
                continue
            found.setdefault(f, set()).add(name)
            todo.extend((f, True, n) for n in own[0])
            for module, exported in own[1]:
                follow(f, root, module, exported)
        elif name in dict(exports):
            todo.append((f, True, dict(exports)[name]))
        elif name in dict(reexports):
            follow(f, root, *dict(reexports)[name])
        elif name != _DEFAULT:
            for module in stars:
                follow(f, root, module, name)
    return {f: frozenset(names) for f, names in found.items()} or None


register_fact(FactSpec(
    # `export *` / `export {…} from`: a barrel that re-exports a route list has no `path` itself
    name=ROUTE_MODULES_FACT, language="typescript",
    gate=(b"react-router", b"path", b"export *", b"export {"),
    collect=_collect_route_modules, resolve=_resolve_route_modules,
))


def _detect(dc: DetectContext) -> Detection | None:
    consts = file_consts(dc.ctx.resolution_index, dc.root, dc.source, dc.path)
    if b"react-router" in dc.source:
        routes = detect_react_routes(dc.root, dc.source, dc.path, const_values=consts)
    else:
        # a list file: only the lists a React Router call takes (a component's
        # `{ path, component }` list, or a Vue / Angular router, has none)
        taken = (index_fact(dc.ctx.resolution_index, ROUTE_MODULES_FACT) or {}).get(dc.path)
        if not taken:
            return None
        lists, _ = _file_lists(dc.root, dc.source)
        routes = []
        for name in sorted(taken):
            arr = lists.get(name)
            if arr is not None and _is_route_array(arr, dc.source):
                _process_config(arr, "", dc.source, dc.path, routes, const_values=consts)
    return Detection(routes, label="react") if routes else None


register_detector(Detector(
    name="react-routes", gate=Gate("typescript", order=52, skip_fixtures=True),
    detect=_detect, frameworks=("react",),
))
