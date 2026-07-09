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

Emits ``semanticType="route"`` statements (``routeKind="page"`` for element routes,
``"mount"`` for ``lazy`` code-split routes). Routes are parented to the file (React
routes are config/markup, not handler methods), mirroring the Angular detector."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..treesitter import node_text


def _join(base: str, sub: str) -> str:
    parts = [p.strip("/") for p in (base, sub) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _string_val(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
        return node_text(frag, source) if frag is not None else ""
    return node_text(node, source).strip("'\"`")


def _component_name(value: Node | None, source: bytes) -> str | None:
    """Component rendered by an ``element`` attr/prop -> its tag name (``<Home/>`` -> ``Home``)."""
    if value is None:
        return None
    node = value
    if node.type == "jsx_expression":  # element={<Home/>}
        node = next((c for c in node.named_children
                     if c.type in ("jsx_self_closing_element", "jsx_element")), None)
    if node is None:
        return None
    if node.type == "jsx_element":
        node = node.child(0)  # opening element
    name = node.child_by_field_name("name") if node is not None else None
    return node_text(name, source) if name is not None else None


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


def _process_jsx(el: Node, prefix: str, source: bytes, path: str, seen: set[str], routes: list[Statement]) -> None:
    attrs = _jsx_attrs(el, source)
    has_path = "path" in attrs
    is_index = "index" in attrs and not has_path  # <Route index .../> -> parent's path
    sub = _string_val(attrs.get("path"), source) if has_path else ""
    full = _join(prefix, sub)
    if has_path or is_index:
        sl, sc = el.start_point[0] + 1, el.start_point[1]
        lazy = "lazy" in attrs
        routes.append(Statement(
            id=disambiguate(statement_id(path, sl, sc), seen),
            parentId=file_id(path),
            nodeType="jsx_element",
            semanticType="route",
            text=node_text(_opening(el), source).split("\n", 1)[0][:120],
            endpoint=full,
            framework="react",
            routeKind="mount" if lazy else "page",
            handler=_component_name(attrs.get("element"), source),
            startLine=sl,
            endLine=el.end_point[0] + 1,
            path=path,
        ))
    # Recurse into nested <Route> children (paired elements only), joining paths.
    if el.type == "jsx_element":
        for child in el.named_children:
            if _is_route(child, source):
                _process_jsx(child, full if has_path else prefix, source, path, seen, routes)


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


def _is_route_array(arr: Node, source: bytes) -> bool:
    return any(e.type == "object" and "path" in _pairs(e, source) for e in arr.named_children)


def _is_children_value(arr: Node, source: bytes) -> bool:
    p = arr.parent
    return p is not None and p.type == "pair" and _key(p, source) == "children"


def _process_config(arr: Node, prefix: str, source: bytes, path: str, seen: set[str], routes: list[Statement]) -> None:
    for elem in arr.named_children:
        if elem.type != "object":
            continue
        pairs = _pairs(elem, source)
        is_index = "index" in pairs and "path" not in pairs  # index route -> parent's path
        if "path" not in pairs and not is_index:
            continue
        full = _join(prefix, "" if is_index else _string_val(pairs["path"], source))
        lazy = "lazy" in pairs
        element = pairs.get("element") or pairs.get("Component") or pairs.get("component")
        sl, sc = elem.start_point[0] + 1, elem.start_point[1]
        routes.append(Statement(
            id=disambiguate(statement_id(path, sl, sc), seen),
            parentId=file_id(path),
            nodeType="object",
            semanticType="route",
            text=node_text(elem, source).split("\n", 1)[0][:120],
            endpoint=full,
            framework="react",
            routeKind="mount" if lazy else "page",
            handler=_component_name(element, source),
            startLine=sl,
            endLine=elem.end_point[0] + 1,
            path=path,
        ))
        children = pairs.get("children")
        if children is not None and children.type == "array":
            _process_config(children, full, source, path, seen, routes)


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
             source: bytes, path: str, seen: set[str], routes: list[Statement]) -> None:
    sl, sc = call.start_point[0] + 1, call.start_point[1]
    routes.append(Statement(
        id=disambiguate(statement_id(path, sl, sc), seen),
        parentId=file_id(path),
        nodeType="call_expression",
        semanticType="route",
        text=node_text(call, source).split("\n", 1)[0][:120],
        endpoint=endpoint,
        framework="react",
        routeKind="page" if kind in ("route", "index") else "mount",
        handler=_string_val(handler_arg, source) or None,
        startLine=sl,
        endLine=call.end_point[0] + 1,
        path=path,
    ))


def _process_v7(node: Node, prefix: str, source: bytes, path: str, seen: set[str],
                routes: list[Statement], consts: dict[str, Node],
                guard: frozenset[str]) -> None:
    """Recursively resolve a v7 route node under the accumulated ``prefix``. Handles the
    array / spread / const-identifier / helper-call forms; ``guard`` breaks const cycles."""
    if node.type == "array":
        for child in node.named_children:
            _process_v7(child, prefix, source, path, seen, routes, consts, guard)
        return
    if node.type == "spread_element":
        inner = node.named_children[0] if node.named_children else None
        if inner is not None:
            _process_v7(inner, prefix, source, path, seen, routes, consts, guard)
        return
    if node.type == "identifier":  # ...groupConst -> resolve to its definition (once)
        name = node_text(node, source)
        target = consts.get(name)
        if target is not None and name not in guard:
            _process_v7(target, prefix, source, path, seen, routes, consts, guard | {name})
        return
    if node.type != "call_expression":
        return
    kind = _callee_name(node, source)
    if kind not in _V7_HELPERS:
        return
    args = _call_args(node)
    if kind == "route":  # route(path, file, opts?)
        seg = _string_val(args[0], source) if args else ""
        handler = args[1] if len(args) > 1 else None
        _emit_v7(node, kind, _join(prefix, seg), handler, source, path, seen, routes)
    elif kind == "index":  # index(file, opts?) -> renders at the parent path
        _emit_v7(node, kind, _join(prefix, ""), args[0] if args else None, source, path, seen, routes)
    elif kind == "layout":  # layout(file, [children]) -> no path segment; recurse
        _emit_v7(node, kind, _join(prefix, ""), args[0] if args else None, source, path, seen, routes)
        children = _last_array(args)
        if children is not None:
            _process_v7(children, prefix, source, path, seen, routes, consts, guard)
    elif kind == "prefix":  # prefix(base, [children]) -> add base, no node of its own
        base = _string_val(args[0], source) if args else ""
        children = _last_array(args)
        if children is not None:
            _process_v7(children, _join(prefix, base), source, path, seen, routes, consts, guard)


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


def _detect_v7(root: Node, source: bytes, path: str, seen: set[str], routes: list[Statement]) -> None:
    # Gate: the route/index/layout/prefix call helpers exist only in v7 framework mode.
    # Keying on the import keeps this inert on v5/v6 (react-router-dom) code.
    if b"@react-router/dev" not in source:
        return
    entry = _v7_entry(root, source)
    if entry is not None:
        _process_v7(entry, "", source, path, seen, routes, _collect_consts(root, source), frozenset())


def detect_react_routes(root: Node, source: bytes, path: str, *, seen_ids: set[str]) -> list[Statement]:
    routes: list[Statement] = []
    # JSX <Route>: start only at top-level Routes (no <Route> ancestor); recursion handles nesting.
    jsx: list[Node] = []
    _walk(root, "jsx_element", jsx)
    _walk(root, "jsx_self_closing_element", jsx)
    for el in jsx:
        if _is_route(el, source) and not _has_route_ancestor(el, source):
            _process_jsx(el, "", source, path, seen_ids, routes)
    # Config objects: route arrays that are not a nested ``children:`` value.
    arrays: list[Node] = []
    _walk(root, "array", arrays)
    for arr in arrays:
        if _is_route_array(arr, source) and not _is_children_value(arr, source):
            _process_config(arr, "", source, path, seen_ids, routes)
    # v7 framework-mode config DSL (gated on the @react-router/dev import).
    _detect_v7(root, source, path, seen_ids, routes)
    return routes
