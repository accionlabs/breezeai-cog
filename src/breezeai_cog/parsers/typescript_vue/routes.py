"""vue-router route detection. Both Vue 2 (``new VueRouter({ routes })``, vue-router 3)
and Vue 3 (``createRouter({ routes })``, vue-router 4) declare routes as the same
config-object array:

    const routes = [
      { path: '/users', name: 'users', component: UserList },
      { path: '/reports', component: () => import('./views/Reports.vue') },  // lazy page
      { path: '/settings', component: Settings, children: [
        { path: 'profile', component: Profile },                            // -> /settings/profile
      ]},
      { path: '/', redirect: '/users' },                                     // navigation
    ]

Emits ``semanticType="route"`` statements (``routeKind="page"``). Nested ``children``
paths join onto the parent. Routes are parented to the file (route config, not handler
methods), mirroring the Angular/React detectors. A route with a ``redirect`` and no component
of its own is a ``routeKind="navigation"`` record — the same model as Angular's ``redirectTo``:
``endpoint`` is the redirect *target* (a ``/``-path without its query or fragment, a
``{ path }`` object's path, or the unique route a ``{ name }`` object names; empty otherwise),
and it has no handler.

Two Vue-specific differences from the Angular detector this borrows from:
  * A Vue route object routinely carries a top-level ``name`` — so ``name`` is NOT an
    exclusion key here (in Angular ``name`` marked a breadcrumb object).
  * A lazy route is ``component: () => import('./X.vue')`` — a page loaded lazily, not a
    child-router mount. So it stays ``routeKind="page"`` with the import target as handler
    (there is no Angular-style ``loadChildren`` mount concept in vue-router)."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import file_id
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import entry_text, node_text
from ..typescript.navigation import literal

# Dynamic-import target: ``() => import('./views/Reports.vue')`` -> ``./views/Reports.vue``
# (an import specifier is a resolvable link; falling back to it beats honest-null here).
_IMPORT_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]""")
_QUERY_OR_FRAGMENT_RE = re.compile(r"[?#]")


def _all(node: Node, typ: str) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == typ:
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(node)
    return out


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


def _string_val(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else ""
    return node_text(node, source)


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _handler(component: Node | None, source: bytes) -> str | None:
    """The component a route renders: a bare identifier (``component: UserList`` ->
    ``UserList``) or a lazy import's specifier (``() => import('./X.vue')`` -> ``./X.vue``).
    Anything else — an inline object, an unresolved expression — → None (honest-null)."""
    if component is None:
        return None
    if component.type == "identifier":
        return node_text(component, source)
    m = _IMPORT_RE.search(node_text(component, source))
    return m.group(1) if m else None


def _default_view(components: Node | None, source: bytes) -> Node | None:
    """Named views (``components: { default: Home, sidebar: Side }``): the page is the
    ``default`` view. No ``default`` key → None (several views, no single page)."""
    if components is None or components.type != "object":
        return None
    for pair in components.named_children:
        if pair.type == "pair" and _key(pair, source) == "default":
            return pair.child_by_field_name("value")
        if pair.type == "shorthand_property_identifier" and node_text(pair, source) == "default":
            return pair
    return None


# For an array to QUALIFY as a route array, at least one element must have 'path' PLUS a
# strongly route-specific render key. Detection now runs on every TS/JS file (no vue-router
# import to gate on — see detect_vue_routes), so the discriminator must be strong enough that
# plain-data arrays are never mistaken for routes:
#   * 'component'/'components' — a rendered component reference; the defining mark of a route.
#   * 'beforeEnter'            — a per-route navigation guard; appears nowhere else.
# Deliberately EXCLUDED as sole qualifiers (they occur in non-route data and would false-match):
#   * 'children'  — file trees, menu/nav configs, org charts all use {path, children}.
#   * 'redirect'  — URL rewrite/redirect maps use {path, redirect}.
#   * 'alias'     — CLI-arg / column configs use {path, alias}.
# Note: 'name' is deliberately absent too — Vue routes legitimately carry a top-level name.
# This only gates ARRAY qualification; once an array qualifies, _process still emits every
# element with a 'path' and recurses through 'children', so nested {path, children} groups
# under a qualifying array are still captured.
_ROUTE_DISCRIMINATING_KEYS = frozenset({"component", "components", "beforeEnter"})


def _is_route_array(arr: Node, source: bytes) -> bool:
    for e in arr.named_children:
        if e.type != "object":
            continue
        keys = _pairs(e, source).keys()
        if "path" in keys and keys & _ROUTE_DISCRIMINATING_KEYS:
            return True
    return False


def _is_children_value(arr: Node, source: bytes) -> bool:
    p = arr.parent
    return p is not None and p.type == "pair" and _key(p, source) == "children"


def _before_enter(value: Node | None, source: bytes) -> list[str]:
    """Names of a route's ``beforeEnter`` guard(s): a function reference or an array of them
    (Vue Router 4). An inline function has no name and contributes nothing."""
    if value is None:
        return []
    items = value.named_children if value.type == "array" else [value]
    return [node_text(i, source) for i in items if i.type in ("identifier", "member_expression")]


#: Router-level guards of one routes array: (beforeEach names, beforeResolve names).
_RouterGuards = tuple[list[str], list[str]]
_NO_ROUTER_GUARDS: _RouterGuards = ([], [])


def _guard_names(arg: Node | None, source: bytes) -> list[str]:
    """A guard function reference's name; an inline function has none (contributes nothing)."""
    if arg is not None and arg.type in ("identifier", "member_expression"):
        return [node_text(arg, source)]
    return []


def _router_guards(root: Node, source: bytes) -> dict[int, _RouterGuards]:
    """Global guards of the routers created in this file, keyed by the start byte of the routes
    array each router serves. ``const router = createRouter({ routes })`` (or ``new
    VueRouter({ routes })``) + ``router.beforeEach(fn)`` / ``router.beforeResolve(fn)`` →
    ``fn`` guards every route of that array. Only a routes array that is inline or a ``const``
    of this file is linked, and only guards registered on the router variable in this file
    count — a guard added in another file (``main.js``) is not certain coverage."""
    if b"beforeEach" not in source and b"beforeResolve" not in source:
        return {}
    arrays: dict[str, Node] = {}  # top-level const name → its array value
    for vd in _all(root, "variable_declarator"):
        name, value = vd.child_by_field_name("name"), vd.child_by_field_name("value")
        if name is not None and value is not None and value.type == "array":
            arrays[node_text(name, source)] = value
    served: dict[str, Node] = {}  # router variable → routes array
    for vd in _all(root, "variable_declarator"):
        name, value = vd.child_by_field_name("name"), vd.child_by_field_name("value")
        if name is None or value is None:
            continue
        if value.type == "call_expression":
            fn = value.child_by_field_name("function")
            ok = fn is not None and node_text(fn, source) == "createRouter"
        elif value.type == "new_expression":
            fn = value.child_by_field_name("constructor")
            ok = fn is not None and node_text(fn, source) == "VueRouter"
        else:
            ok = False
        args = value.child_by_field_name("arguments") if ok else None
        config = next((a for a in args.named_children if a.type == "object"), None) if args else None
        if config is None:
            continue
        for c in config.named_children:
            if c.type == "shorthand_property_identifier" and node_text(c, source) == "routes":
                target: Node | None = arrays.get("routes")
            elif c.type == "pair" and _key(c, source) == "routes":
                v = c.child_by_field_name("value")
                target = v if v is not None and v.type == "array" else (
                    arrays.get(node_text(v, source)) if v is not None and v.type == "identifier" else None)
            else:
                continue
            if target is not None:
                served[node_text(name, source)] = target
    out: dict[int, _RouterGuards] = {}
    for call in _all(root, "call_expression"):
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
        hook = node_text(prop, source) if prop is not None else ""
        target = served.get(node_text(obj, source)) if obj is not None else None
        if hook not in ("beforeEach", "beforeResolve") or target is None:
            continue
        args = call.child_by_field_name("arguments")
        names = _guard_names(args.named_children[0] if args and args.named_children else None, source)
        before, after = out.setdefault(target.start_byte, ([], []))
        (before if hook == "beforeEach" else after).extend(names)
    return out


def _has_empty_path_child(children: Node | None, source: bytes) -> bool:
    """Whether a ``children`` array holds a route object with ``path: ''``."""
    if children is None or children.type != "array":
        return False
    for child in children.named_children:
        if child.type == "object":
            p = _pairs(child, source).get("path")
            if p is not None and p.type == "string" and _string_val(p, source) == "":
                return True
    return False


#: Route name → full path of the one route with that name in this file; ``None`` when the name
#: is declared more than once or the route's own path is not a literal, so no single page is
#: meant (an ancestor's path is composed as the page record composes it).
_RouteNames = dict[str, str | None]
_NO_NAMES: _RouteNames = {}


def _collect_names(elem: Node, prefix: str, source: bytes, names: _RouteNames) -> None:
    """Record ``name`` → full path of this route object and, recursively, of its children."""
    if elem.type != "object":
        return
    pairs = _pairs(elem, source)
    path = pairs.get("path")
    if path is None:
        return
    full = _join(prefix, _string_val(path, source))
    name = pairs.get("name")
    if name is not None and name.type == "string":
        key = _string_val(name, source)
        names[key] = full if key not in names and path.type == "string" else None
    children = pairs.get("children")
    if children is not None and children.type == "array":
        for child in children.named_children:
            _collect_names(child, full, source, names)


def _route_names(arrays: list[Node], objects: list[Node], source: bytes) -> _RouteNames:
    """Names of the routes in this file's route arrays and single route-object modules."""
    names: _RouteNames = {}
    for elem in [e for arr in arrays for e in arr.named_children] + objects:
        _collect_names(elem, "", source, names)
    return names


def _absolute_target(text: str | None) -> str | None:
    """A redirect target written as an absolute path, normalized like a page endpoint and without
    its query string or fragment (page data, not part of the address). A relative one depends on
    the parent route: empty."""
    if text is None or not text.startswith("/"):
        return None
    return _join("", _QUERY_OR_FRAGMENT_RE.split(text, maxsplit=1)[0])


def _redirect_endpoint(value: Node, source: bytes, names: _RouteNames) -> str | None:
    """Where a ``redirect`` sends the user, as a full route path: ``'/home'``, ``{ path:
    '/home' }``, or ``{ name: 'home' }`` (the path of the one route with that name). A function,
    a relative path, a variable or anything else → ``None`` (never a guess)."""
    if value.type != "object":
        return _absolute_target(literal(value, source))
    pairs = _pairs(value, source)
    target, name = pairs.get("path"), pairs.get("name")
    if target is not None:
        return _absolute_target(literal(target, source))
    if name is not None and name.type == "string":
        return names.get(_string_val(name, source))
    return None


def _redirect_record(elem: Node, source: bytes, path: str, names: _RouteNames,
                     redirect: Node) -> Emit:
    """The navigation record of a pure redirect route: no handler, its own source as ``text``."""
    return Emit(
        elem,
        {
            "semanticType": "route",
            "endpoint": _redirect_endpoint(redirect, source, names),
            "framework": "vue",
            "routeKind": "navigation",
        },
        parent=file_id(path), text=entry_text(elem, source),
    )


def _emit(
    elem: Node, prefix: str, source: bytes, path: str, routes: list[Emit],
    outer: _RouterGuards = _NO_ROUTER_GUARDS, inherited: tuple[str, ...] = (),
    names: _RouteNames = _NO_NAMES,
) -> None:
    """Emit ONE route object and recurse into its ``children``: a page record for a route that
    shows a component, a navigation record for a pure redirect.
    Shared by the array walk (``_process``) and the single-object module form. ``outer`` are the
    router's global guards: ``beforeEach`` runs before the route's ``beforeEnter``,
    ``beforeResolve`` after it (vue-router's navigation order). ``inherited`` are the ancestors'
    ``beforeEnter``: entering a child enters its parents first, so theirs run before its own.

    A path-only parent ``{ path, children }`` and a layout (a component whose children include
    an empty-path child, which always fills its outlet) are wiring (contract §4): no record,
    their path prefixes the children and their ``beforeEnter`` guards them."""
    if elem.type != "object":
        return
    pairs = _pairs(elem, source)
    if "path" not in pairs:
        return
    full = _join(prefix, _string_val(pairs["path"], source))
    # A page is a route with a component of its own that is not a layout (a path-only parent is
    # a prefix). A pure redirect (no component) is an outbound navigation to its target, like
    # Angular's redirectTo. A shell with a component and a redirect (its default child) is still
    # a page only. The children are walked in every case — they are often the whole app subtree.
    has_component = "component" in pairs or "components" in pairs
    children = pairs.get("children")
    own = _before_enter(pairs.get("beforeEnter"), source)
    redirect = pairs.get("redirect")
    if redirect is not None and not has_component:
        routes.append(_redirect_record(elem, source, path, names, redirect))
    if has_component and not _has_empty_path_child(children, source):
        routes.append(Emit(
            elem,
            {
                "semanticType": "route",
                "endpoint": full,
                "framework": "vue",
                "routeKind": "page",
                "handler": _handler(
                    pairs.get("component") or _default_view(pairs.get("components"), source), source
                ),
                "guards": (outer[0] + list(inherited) + own + outer[1]) or None,
            },
            parent=file_id(path), text=entry_text(elem, source),
        ))
    if children is not None and children.type == "array":
        for child in children.named_children:
            _emit(child, full, source, path, routes, outer, (*inherited, *own), names)


def _process(
    arr: Node, prefix: str, source: bytes, path: str, routes: list[Emit],
    outer: _RouterGuards = _NO_ROUTER_GUARDS, names: _RouteNames = _NO_NAMES,
) -> None:
    for elem in arr.named_children:
        _emit(elem, prefix, source, path, routes, outer, names=names)


def _is_route_object(obj: Node, source: bytes) -> bool:
    """A single object that is itself a route (a ``router/modules/*`` fragment: ``const r =
    { path, component, children } ; export default r``). Same discriminator as the array form."""
    keys = _pairs(obj, source).keys()
    return "path" in keys and bool(keys & _ROUTE_DISCRIMINATING_KEYS)


def _module_route_objects(root: Node, source: bytes) -> list[Node]:
    """Top-level route OBJECTS not wrapped in a route array — the module-split pattern where a
    file declares/exports a single route object (``export default { path, component, children }``
    or ``const r = {…}; export default r``). Only module-level declarations/exports are scanned,
    so nested objects (already reached via the array walk / children recursion) are not
    re-processed."""
    out: list[Node] = []
    for child in root.named_children:
        decls: list[Node] = []
        if child.type in ("lexical_declaration", "variable_declaration"):
            decls = [child]
        elif child.type == "export_statement":
            for c in child.named_children:
                if c.type == "object" and _is_route_object(c, source):  # export default {…}
                    out.append(c)
                elif c.type in ("lexical_declaration", "variable_declaration"):
                    decls.append(c)  # export const r = {…}
        for d in decls:
            for vd in d.named_children:
                if vd.type != "variable_declarator":
                    continue
                value = vd.child_by_field_name("value")
                if value is not None and value.type == "object" and _is_route_object(value, source):
                    out.append(value)
    return out


# Angular and React also describe routes as {path, component, …} config arrays — an identical
# shape. They are import-gated one-per-file parsers with their own route detectors, so this
# additive pass defers to them (their signal is reliable) rather than double-emitting the same
# array under framework="vue". No such collision exists with call-shaped route frameworks
# (Express `app.get()`) or decorator-shaped ones (Nest `@Get()`) — different syntax entirely.
_COMPETING_CONFIG_ROUTERS = (b"@angular/", b"react-router")


def detect_vue_routes(root: Node, source: bytes, path: str) -> list[Emit]:
    # Vue route configs are plain-data arrays that often live in files importing nothing from
    # vue-router (a default-export array, a `router/modules/*.js` fragment mounted elsewhere).
    # So detection can't gate on an import — the ARRAY SHAPE is the signal. A cheap structural
    # byte pre-guard short-circuits the AST walk for the ~all files that can't hold a route
    # array: it must mention a `path` and a route-specific render key.
    if any(m in source for m in _COMPETING_CONFIG_ROUTERS):
        return []
    if b"path" not in source or (b"component" not in source and b"beforeEnter" not in source):
        return []
    routes: list[Emit] = []
    router_guards = _router_guards(root, source)
    arrays = [a for a in _all(root, "array")
              if _is_route_array(a, source) and not _is_children_value(a, source)]
    # Single route OBJECT modules (const r = {…}; export default r) — not reachable via the
    # array walk. Elements referenced by identifier inside a routes array are skipped there
    # (not objects), and captured here from their own declaration, so there is no double-emit.
    objects = _module_route_objects(root, source)
    names = _route_names(arrays, objects, source) if b"redirect" in source else _NO_NAMES
    for arr in arrays:
        _process(arr, "", source, path, routes,
                 router_guards.get(arr.start_byte, _NO_ROUTER_GUARDS), names)
    for obj in objects:
        _emit(obj, "", source, path, routes, names=names)
    return routes


def _detect(dc: DetectContext) -> Detection | None:
    routes = detect_vue_routes(dc.root, dc.source, dc.path)
    return Detection(routes, label="vue") if routes else None


register_detector(Detector(
    name="vue-routes", gate=Gate("typescript", order=50, skip_fixtures=True), detect=_detect,
    frameworks=("vue",),
))
