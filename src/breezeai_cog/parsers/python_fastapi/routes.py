"""FastAPI route detection — finds ``@app.get("/x")`` / ``@router.post(...)`` style
decorators and emits ``semanticType="route"`` statements parented to their handler
function (via the shared id convention, so the parentId matches the function id the
Python extraction assigned).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, function_id, statement_id
from ...schemas import Statement
from ..treesitter import node_text

# HTTP verbs (+ websocket) exposed as decorator methods on FastAPI app/router objects.
_VERBS = {"get", "post", "put", "patch", "delete", "options", "head", "trace", "websocket"}

#: A prefix that exists but has no resolvable value (``prefix=settings.API_PREFIX``): the
#: bindings behind it have an unknown address, so their endpoint is empty.
_UNKNOWN = object()


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


def _literal(node: Node | None, source: bytes) -> str | None:
    """A plain string literal's value (no f-string interpolation), else None."""
    if node is None or node.type != "string":
        return None
    if any(c.type == "interpolation" for c in node.named_children):
        return None
    content = next((c for c in node.named_children if c.type == "string_content"), None)
    return node_text(content, source) if content is not None else ""


def _prefix_kwarg(call: Node, source: bytes) -> str | object:
    """The ``prefix=`` keyword of an ``APIRouter(...)`` / ``include_router(...)`` call: its literal
    value, ``""`` when absent, or :data:`_UNKNOWN` when present but not a literal (Python has no
    compile-time constants, so a name or attribute has no value cog can trust)."""
    args = call.child_by_field_name("arguments")
    for arg in args.named_children if args is not None else []:
        key = arg.child_by_field_name("name") if arg.type == "keyword_argument" else None
        if key is not None and node_text(key, source) == "prefix":
            value = _literal(arg.child_by_field_name("value"), source)
            return value if value is not None else _UNKNOWN
    return ""


def _calls(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "call":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


_DEPENDS = frozenset({"Depends", "Security", "fastapi.Depends", "fastapi.Security"})
_ROUTERS = frozenset({"APIRouter", "fastapi.APIRouter", "FastAPI", "fastapi.FastAPI"})


def _dependencies(call: Node, source: bytes) -> list[str]:
    """Guard names from a ``dependencies=[Depends(fn), Security(fn, scopes=…)]`` keyword: the
    dependency callable of each entry (``fn`` / ``auth.require_user``). An entry that is not a
    ``Depends``/``Security`` call on a name is skipped (no fabricated name)."""
    args = call.child_by_field_name("arguments")
    for arg in args.named_children if args is not None else []:
        key = arg.child_by_field_name("name") if arg.type == "keyword_argument" else None
        if key is None or node_text(key, source) != "dependencies":
            continue
        value = arg.child_by_field_name("value")
        names: list[str] = []
        for el in value.named_children if value is not None and value.type in ("list", "tuple") else []:
            fn = el.child_by_field_name("function") if el.type == "call" else None
            dep_args = el.child_by_field_name("arguments") if fn is not None else None
            first = next((a for a in dep_args.named_children if a.type != "keyword_argument"), None) \
                if dep_args is not None else None
            if (fn is not None and node_text(fn, source) in _DEPENDS and first is not None
                    and first.type in ("identifier", "attribute")):
                names.append(node_text(first, source))
        return names
    return []


def _router_bases(
    root: Node, source: bytes
) -> tuple[dict[str, str | object], dict[str, list[str]]]:
    """Router / app variable → (the prefix its routes are served under, the guards its routes
    inherit), from this file's own wiring: ``router = APIRouter(prefix="/users",
    dependencies=[Depends(auth)])`` and ``app.include_router(router, prefix=…, dependencies=…)``
    (nested includes compose, outer first; FastAPI joins prefixes by plain concatenation).
    ``app = FastAPI(dependencies=…)`` gives every route of the app its guards.

    A router never included in this file keeps its own prefix and guards (no wiring seen). A
    router included at more than one place keeps its own too (ambiguous). A prefix that exists
    but is not a literal makes the base :data:`_UNKNOWN`. Includes of routers from other files
    (``app.include_router(orders.router)``) are not resolved here."""
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

    def wiring(name: str, seen: frozenset[str]) -> tuple[str | object, list[str]]:
        prefix, deps = own.get(name, ""), own_deps.get(name, [])
        sites = includes.get(name, set())
        if len(sites) != 1 or name in seen:  # not included here, or ambiguous, or a cycle
            return prefix, deps
        (parent, mount, mount_deps), = sites
        parent_base, parent_deps = wiring(parent, seen | {name})
        guards = parent_deps + list(mount_deps) + deps
        if _UNKNOWN in (parent_base, mount, prefix):
            return _UNKNOWN, guards
        return f"{parent_base}{mount}{prefix}", guards

    resolved = {name: wiring(name, frozenset()) for name in own}
    return ({k: v[0] for k, v in resolved.items()},
            {k: v[1] for k, v in resolved.items()})


def _routes(dec: Node, fdef: Node, source: bytes, path: str, class_name: str | None,
            seen_ids: set[str], bases: dict[str, str | object],
            router_guards: dict[str, list[str]]) -> list[Statement]:
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
    start_line, start_col = dec.start_point[0] + 1, dec.start_point[1]
    endpoint = _endpoint(call, source)
    receiver = func.child_by_field_name("object")
    receiver_name = node_text(receiver, source) if receiver is not None else ""
    served = bases.get(receiver_name, "")
    # Guards: the router's / app's inherited dependencies, then the route's own (outer first).
    guards = router_guards.get(receiver_name, []) + _dependencies(call, source)
    if served is _UNKNOWN:
        endpoint = None  # a prefix we cannot resolve: the address is unknown
    elif endpoint is not None and isinstance(served, str):
        endpoint = (served + endpoint) or None
    text = node_text(dec, source).split("\n", 1)[0]
    parent = function_id(path, name, handler_line, class_name=class_name)
    return [
        Statement(
            id=disambiguate(statement_id(path, start_line, start_col), seen_ids),
            parentId=parent,
            nodeType="synthetic",
            semanticType="route",
            text=text,
            method=m,
            endpoint=endpoint,
            framework="fastapi",
            handler=name,
            handlerLine=handler_line,
            guards=guards or None,
            # Honest-null: assert auth only when a guard is present; absence is unknown.
            authRequired=True if guards else None,
            routeKind="ws" if m == "WEBSOCKET" else "route",
            startLine=start_line,
            endLine=dec.end_point[0] + 1,
            path=path,
        )
        for m in methods
    ]


def detect_routes(root: Node, source: bytes, path: str, *, seen_ids: set[str]) -> list[Statement]:
    """Walk the tree (tracking the enclosing class) and emit route statements."""
    routes: list[Statement] = []
    bases, router_guards = (
        _router_bases(root, source) if b"APIRouter" in source or b"FastAPI(" in source
        else ({}, {})
    )

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
                        routes.extend(_routes(dec, fdef, source, path, class_name, seen_ids,
                                              bases, router_guards))
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
