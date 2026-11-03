"""FastAPI route detection — finds ``@app.get("/x")`` / ``@router.post(...)`` style
decorators and emits ``semanticType="route"`` statements parented to their handler
function (via the shared id convention, so the parentId matches the function id the
Python extraction assigned).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import function_id
from ..emits import Emit
from ..treesitter import node_text
from .file_facts import ROUTERS as _ROUTERS
from .file_facts import UNKNOWN as _UNKNOWN
from .file_facts import app_middleware as _app_middleware
from .file_facts import calls as _calls
from .file_facts import dependencies as _dependencies
from .file_facts import literal as _literal
from .file_facts import mounted_names as _mounted_names
from .file_facts import prefix_kwarg as _prefix_kwarg
from .auth import certain_auth
from .wiring import fastapi_wiring

# HTTP verbs (+ websocket) exposed as decorator methods on FastAPI app/router objects.
_VERBS = {"get", "post", "put", "patch", "delete", "options", "head", "trace", "websocket"}

def _name(node: Node, source: bytes) -> str:
    n = node.child_by_field_name("name")
    return node_text(n, source) if n is not None else ""


def _endpoint(call: Node, source: bytes) -> str | None:
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    for arg in args.named_children:
        if arg.type == "string":  # "" is the router's prefix itself (``@router.get("")``)
            return _literal(arg, source)
    return None


def _methods_kwarg(call: Node, source: bytes) -> list[str]:
    """HTTP verbs from an ``api_route(..., methods=["GET", "POST"])`` keyword arg."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return []
    for arg in args.named_children:
        if arg.type != "keyword_argument":
            continue
        key = arg.child_by_field_name("name")
        if key is None or node_text(key, source) != "methods":
            continue
        val = arg.child_by_field_name("value")
        if val is None or val.type not in ("list", "tuple", "set"):
            return []
        out = []
        for el in val.named_children:
            if el.type == "string":
                content = next((c for c in el.named_children if c.type == "string_content"), None)
                if content is not None:
                    out.append(node_text(content, source).upper())
        return out
    return []


def _router_bases(
    root: Node, source: bytes
) -> tuple[dict[str, str | object], dict[str, list[str]], dict[str, str]]:
    """Router / app variable → (the prefix its routes are served under, the guards its routes
    inherit), from this file's own wiring: ``router = APIRouter(prefix="/users",
    dependencies=[Depends(auth)])`` and ``app.include_router(router, prefix=…, dependencies=…)``
    (nested includes compose, outer first; FastAPI joins prefixes by plain concatenation).
    ``app = FastAPI(dependencies=…)`` gives every route of the app its guards.

    A router never included in this file keeps its own prefix and guards (no wiring seen). A
    router included at more than one place is served at each: its base is :data:`_UNKNOWN`
    (never its own prefix alone, an address it is not served at). A prefix that exists
    but is not a literal makes the base :data:`_UNKNOWN`. Includes of routers from other files
    (``app.include_router(orders.router)``) are not resolved here. The third map gives each
    router the outermost app/router it is reached from in this file (itself when not included)."""
    own: dict[str, str | object] = {}
    own_deps: dict[str, list[str]] = {}
    for node in _calls(root):
        fn = node.child_by_field_name("function")
        parent = node.parent
        if (fn is not None and node_text(fn, source) in _ROUTERS
                and parent is not None and parent.type == "assignment"):
            target = parent.child_by_field_name("left")
            if target is not None and target.type == "identifier":
                own[node_text(target, source)] = _prefix_kwarg(node, source)
                own_deps[node_text(target, source)] = _dependencies(node, source)
    includes: dict[str, set[tuple[str, object, tuple[str, ...]]]] = {}
    for node in _calls(root):
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "attribute":
            continue
        attr = fn.child_by_field_name("attribute")
        obj = fn.child_by_field_name("object")
        args = node.child_by_field_name("arguments")
        first = next((a for a in args.named_children if a.type != "keyword_argument"), None) \
            if args is not None else None
        if (attr is None or obj is None or node_text(attr, source) != "include_router"
                or first is None or first.type != "identifier"
                or node_text(first, source) not in own):
            continue
        includes.setdefault(node_text(first, source), set()).add(
            (node_text(obj, source), _prefix_kwarg(node, source),
             tuple(_dependencies(node, source))))

    def wiring(name: str, seen: frozenset[str]) -> tuple[str | object, list[str], str]:
        prefix, deps = own.get(name, ""), own_deps.get(name, [])
        sites = includes.get(name, set())
        if len(sites) > 1:  # served under each include: no single address
            return _UNKNOWN, deps, name
        if not sites or name in seen:  # not included here, or a cycle
            return prefix, deps, name
        (parent, mount, mount_deps), = sites
        parent_base, parent_deps, top = wiring(parent, seen | {name})
        guards = parent_deps + list(mount_deps) + deps
        if _UNKNOWN in (parent_base, mount, prefix):
            return _UNKNOWN, guards, top
        return f"{parent_base}{mount}{prefix}", guards, top

    # A sub-application mounted in this file (``app.mount("/v1", sub)``): served under a path
    # this pass does not follow, so its address is unknown.
    for name in _mounted_names(root, source) if b".mount(" in source else ():
        if name in own:
            own[name] = _UNKNOWN
    resolved = {name: wiring(name, frozenset()) for name in own}
    return ({k: v[0] for k, v in resolved.items()},
            {k: v[1] for k, v in resolved.items()},
            {k: v[2] for k, v in resolved.items()})


def _routes(dec: Node, fdef: Node, source: bytes, path: str, class_name: str | None,
            bases: dict[str, str | object],
            router_guards: dict[str, list[str]], roots: dict[str, str],
            middleware: dict[str, list[str]], auth: frozenset[str] = frozenset()) -> list[Emit]:
    call = dec.named_children[0] if dec.named_children else None
    if call is None or call.type != "call":
        return []
    func = call.child_by_field_name("function")
    if func is None or func.type != "attribute":  # need X.verb(...)
        return []
    verb_node = func.named_children[-1] if func.named_children else None
    if verb_node is None:
        return []
    verb = node_text(verb_node, source).lower()
    if verb in _VERBS:
        methods = [verb.upper()]
    elif verb == "api_route":
        # generic `@router.api_route("/x", methods=[...])` -> one route per verb (GET default)
        methods = _methods_kwarg(call, source) or ["GET"]
    else:
        return []

    name = _name(fdef, source)
    handler_line = fdef.start_point[0] + 1
    endpoint = _endpoint(call, source)
    receiver = func.child_by_field_name("object")
    receiver_name = node_text(receiver, source) if receiver is not None else ""
    served = bases.get(receiver_name, "")
    # Guards: the app's middleware (decision Q9: a global guard on every binding it covers),
    # then the router's / app's inherited dependencies, then the route's own (outer first).
    # Only a security-scheme dependency asserts auth (:mod:`.auth`): middleware is often logging
    # or CORS, a dependency often a DB session or paging.
    scoped = router_guards.get(receiver_name, []) + _dependencies(call, source)
    guards = middleware.get(roots.get(receiver_name, receiver_name), []) + scoped
    if served is _UNKNOWN:
        endpoint = None  # a prefix we cannot resolve: the address is unknown
    elif endpoint is not None and isinstance(served, str):
        endpoint = (served + endpoint) or None
    parent = function_id(path, name, handler_line, class_name=class_name)
    return [
        Emit(dec, dict(
            semanticType="route",
            method=m,
            endpoint=endpoint,
            framework="fastapi",
            handler=name,
            handlerLine=handler_line,
            guards=guards or None,
            # Only a dependency that certainly rejects an anonymous call asserts auth
            # (:mod:`.auth`); any other is listed, its effect unknown.
            authRequired=True if auth.intersection(scoped) else None,
            routeKind="ws" if m == "WEBSOCKET" else "route",
        ), parent=parent, node_type="synthetic")
        for m in methods
    ]


def _from_index(
    index: object | None, path: str
) -> tuple[dict[str, str | object], dict[str, list[str]], dict[str, str], dict[str, list[str]]] | None:
    """This file's router wiring from the ``fastapi.wiring`` fact, which also follows
    ``include_router`` across files: (bases, inherited guards, roots, middleware by root).
    A root in another file is keyed ``file::var`` so it never collides with a local name. None
    when there is no index or it has no entry for this file."""
    wiring = fastapi_wiring(index)
    mine = wiring.routers.get(path) if wiring is not None else None
    if wiring is None or not mine:
        return None
    all_middleware = wiring.middleware
    bases: dict[str, str | object] = {}
    guards: dict[str, list[str]] = {}
    roots: dict[str, str] = {}
    middleware: dict[str, list[str]] = {}
    for var, (served, deps, (top_file, top_var)) in mine.items():
        bases[var] = _UNKNOWN if served is None else served
        guards[var] = list(deps)
        key = top_var if top_file == path else f"{top_file}::{top_var}"
        roots[var] = key
        middleware[key] = list(all_middleware.get(top_file, {}).get(top_var, []))
    return bases, guards, roots, middleware


def detect_routes(
    root: Node, source: bytes, path: str, *, index: object | None = None
) -> list[Emit]:
    """Walk the tree (tracking the enclosing class) and return its routes, for
    :func:`~..emits.apply`. ``index`` is the Python repo index: when given, router wiring comes
    from its ``fastapi.wiring`` fact (which also follows ``include_router`` across files);
    without it, only this file's wiring is seen."""
    routes: list[Emit] = []
    from_index = _from_index(index, path)
    if from_index is not None:
        bases, router_guards, roots, middleware = from_index
    else:
        bases, router_guards, roots = (
            _router_bases(root, source) if b"APIRouter" in source or b"FastAPI(" in source
            else ({}, {}, {})
        )
        middleware = _app_middleware(root, source) if b"middleware" in source else {}

    auth = certain_auth(root, source)

    def walk(node: Node, class_name: str | None) -> None:
        for child in node.named_children:
            if child.type == "class_definition":
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, _name(child, source))
            elif child.type == "decorated_definition":
                fdef = next((c for c in child.named_children if c.type == "function_definition"), None)
                cdef = next((c for c in child.named_children if c.type == "class_definition"), None)
                if cdef is not None:
                    body = cdef.child_by_field_name("body")
                    if body is not None:
                        walk(body, _name(cdef, source))
                elif fdef is not None:
                    for dec in (c for c in child.named_children if c.type == "decorator"):
                        routes.extend(_routes(dec, fdef, source, path, class_name,
                                              bases, router_guards, roots, middleware, auth))
                    body = fdef.child_by_field_name("body")
                    if body is not None:
                        walk(body, class_name)
            elif child.type == "function_definition":
                body = child.child_by_field_name("body")
                if body is not None:
                    walk(body, class_name)
            else:
                walk(child, class_name)

    walk(root, None)
    return routes
