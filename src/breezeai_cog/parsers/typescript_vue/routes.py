"""vue-router route detection. Both Vue 2 (``new VueRouter({ routes })``, vue-router 3)
and Vue 3 (``createRouter({ routes })``, vue-router 4) declare routes as the same
config-object array:

    const routes = [
      { path: '/users', name: 'users', component: UserList },
      { path: '/reports', component: () => import('./views/Reports.vue') },  // lazy page
      { path: '/settings', component: Settings, children: [
        { path: 'profile', component: Profile },                            // -> /settings/profile
      ]},
      { path: '/', redirect: '/users' },                                     // skipped
    ]

Emits ``semanticType="route"`` statements (``routeKind="page"``). Nested ``children``
paths join onto the parent. Routes are parented to the file (route config, not handler
methods), mirroring the Angular/React detectors.

Two Vue-specific differences from the Angular detector this borrows from:
  * A Vue route object routinely carries a top-level ``name`` — so ``name`` is NOT an
    exclusion key here (in Angular ``name`` marked a breadcrumb object).
  * A lazy route is ``component: () => import('./X.vue')`` — a page loaded lazily, not a
    child-router mount. So it stays ``routeKind="page"`` with the import target as handler
    (there is no Angular-style ``loadChildren`` mount concept in vue-router)."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..additive import DetectContext, Detector, Gate, register_detector
from ..treesitter import entry_text, node_text

# Dynamic-import target: ``() => import('./views/Reports.vue')`` -> ``./views/Reports.vue``
# (an import specifier is a resolvable link; falling back to it beats honest-null here).
_IMPORT_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]""")


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


def _emit(
    elem: Node, prefix: str, source: bytes, path: str, seen: set[str], routes: list[Statement],
    outer: _RouterGuards = _NO_ROUTER_GUARDS,
) -> None:
    """Emit ONE route object (if it isn't a pure redirect) and recurse into its ``children``.
    Shared by the array walk (``_process``) and the single-object module form. ``outer`` are the
    router's global guards: ``beforeEach`` runs before the route's ``beforeEnter``,
    ``beforeResolve`` after it (vue-router's navigation order)."""
    if elem.type != "object":
        return
    pairs = _pairs(elem, source)
    if "path" not in pairs:
        return
    full = _join(prefix, _string_val(pairs["path"], source))
    # Emit unless this is a PURE redirect (a redirect with no component of its own is an
    # alias, not a page — like Angular's redirectTo). A Vue LAYOUT route idiomatically has
    # BOTH a component (the shell) and a redirect (its default child): it is a real page,
    # and — critically — its `children` are the whole app subtree. We must still recurse
    # into them even when the parent is a pure redirect, so the skip only gates emission.
    is_pure_redirect = (
        "redirect" in pairs and "component" not in pairs and "components" not in pairs
    )
    if not is_pure_redirect:
        sl, sc = elem.start_point[0] + 1, elem.start_point[1]
        routes.append(
            Statement(
                id=disambiguate(statement_id(path, sl, sc), seen),
                parentId=file_id(path),
                nodeType="synthetic",
                semanticType="route",
                text=entry_text(elem, source),
                endpoint=full,
                framework="vue",
                routeKind="page",
                handler=_handler(
                    pairs.get("component") or _default_view(pairs.get("components"), source), source
                ),
                guards=(outer[0] + _before_enter(pairs.get("beforeEnter"), source) + outer[1])
                or None,
                startLine=sl,
                endLine=elem.end_point[0] + 1,
                path=path,
            )
        )
    children = pairs.get("children")
    if children is not None and children.type == "array":
        for child in children.named_children:
            _emit(child, full, source, path, seen, routes, outer)


def _process(
    arr: Node, prefix: str, source: bytes, path: str, seen: set[str], routes: list[Statement],
    outer: _RouterGuards = _NO_ROUTER_GUARDS,
) -> None:
    for elem in arr.named_children:
        _emit(elem, prefix, source, path, seen, routes, outer)


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


def detect_vue_routes(
    root: Node, source: bytes, path: str, *, seen_ids: set[str], index: object = None
) -> list[Statement]:
    # Vue route configs are plain-data arrays that often live in files importing nothing from
    # vue-router (a default-export array, a `router/modules/*.js` fragment mounted elsewhere).
    # So detection can't gate on an import — the ARRAY SHAPE is the signal. A cheap structural
    # byte pre-guard short-circuits the AST walk for the ~all files that can't hold a route
    # array: it must mention a `path` and a route-specific render key.
    if any(m in source for m in _COMPETING_CONFIG_ROUTERS):
        return []
    if b"path" not in source or (b"component" not in source and b"beforeEnter" not in source):
        return []
    routes: list[Statement] = []
    router_guards = _router_guards(root, source)
    for arr in _all(root, "array"):
        if _is_route_array(arr, source) and not _is_children_value(arr, source):
            _process(arr, "", source, path, seen_ids, routes,
                     router_guards.get(arr.start_byte, _NO_ROUTER_GUARDS))
    # Single route OBJECT modules (const r = {…}; export default r) — not reachable via the
    # array walk. Elements referenced by identifier inside a routes array are skipped there
    # (not objects), and captured here from their own declaration, so there is no double-emit.
    for obj in _module_route_objects(root, source):
        _emit(obj, "", source, path, seen_ids, routes)
    return routes


def _run(dc: DetectContext) -> str | None:
    routes = detect_vue_routes(
        dc.root, dc.source, dc.path, seen_ids={s.id for s in dc.record.statements}
    )
    if not routes:
        return None
    dc.record.statements.extend(routes)
    return "vue"


register_detector(Detector(
    name="vue-routes", gate=Gate("typescript", order=50, skip_fixtures=True), run=_run,
    frameworks=("vue",),
))
