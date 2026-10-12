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


def _router_bases(root: Node, source: bytes) -> dict[str, str | object]:
    """Router / app variable → the prefix its routes are served under, from this file's own
    wiring: ``router = APIRouter(prefix="/users")`` and ``app.include_router(router, prefix=…)``
    (nested includes compose; FastAPI joins prefixes by plain concatenation).

    A router never included in this file keeps its own prefix (no wiring seen). A router
    included at more than one place keeps its own prefix too (ambiguous). A prefix that exists
    but is not a literal makes the base :data:`_UNKNOWN`. Includes of routers from other files
    (``app.include_router(orders.router)``) are not resolved here."""
    own: dict[str, str | object] = {}
    for node in _calls(root):
        fn = node.child_by_field_name("function")
        parent = node.parent
        if (fn is not None and node_text(fn, source) in ("APIRouter", "fastapi.APIRouter")
                and parent is not None and parent.type == "assignment"):
            target = parent.child_by_field_name("left")
            if target is not None and target.type == "identifier":
                own[node_text(target, source)] = _prefix_kwarg(node, source)
    includes: dict[str, set[tuple[str, object]]] = {}
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
            (node_text(obj, source), _prefix_kwarg(node, source)))

    def base(name: str, seen: frozenset[str]) -> str | object:
        prefix = own.get(name, "")
        sites = includes.get(name, set())
        if len(sites) != 1 or name in seen:  # not included here, or ambiguous, or a cycle
            return prefix
        (parent, mount), = sites
        parent_base = base(parent, seen | {name})
        if _UNKNOWN in (parent_base, mount, prefix):
            return _UNKNOWN
        return f"{parent_base}{mount}{prefix}"

    return {name: base(name, frozenset()) for name in own}


def _routes(dec: Node, fdef: Node, source: bytes, path: str, class_name: str | None,
            seen_ids: set[str], bases: dict[str, str | object]) -> list[Statement]:
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
    served = bases.get(node_text(receiver, source), "") if receiver is not None else ""
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
    bases = _router_bases(root, source) if b"APIRouter" in source else {}

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
                        routes.extend(_routes(dec, fdef, source, path, class_name, seen_ids, bases))
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
