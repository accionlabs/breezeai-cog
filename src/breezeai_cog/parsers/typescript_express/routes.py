"""Express route detection (spec A4). Express is call-based — routes are registered
by calling an HTTP-verb method on an ``app`` / ``Router`` object:

  app.get('/users/:id', handler)   router.post('/users', handler)   → route
  app.use('/api', router)                                            → route (mount)
  app.route('/book')  (chained .get()/.post())                       → route (group)

Per the contract (Part C / B1.4), a detection sets ``semanticType`` on the **same span**:
where the base parser already captured the enclosing statement (a top-level
``expression_statement``) we enrich it in place; for calls inside handler/callback
scopes — which the base skips as a nested scope — we add a statement parented to the
enclosing function. Mutates ``record`` (mirrors ``java_vertx``, the other call-based
detector)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..treesitter import first_line, node_text

# HTTP-verb methods that register a route handler on an app/router.
_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "options", "head", "all"}


def _invocations(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "call_expression":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _is_router_obj(obj_text: str) -> bool:
    """Cheap heuristic: is this call's receiver an Express app / router?
    Handles the named-variable forms (``app`` / ``router`` / ``this.router`` /
    ``userRouter`` / ``apiApp``) and the direct-constructor forms
    (``Router().use(...)`` / ``express.Router().get(...)``)."""
    low = obj_text.lower()
    tail = low.rsplit(".", 1)[-1].strip()
    if tail in {"app", "router", "server", "api", "route"} or tail.endswith(("router", "app")):
        return True
    return "router()" in low or "express()" in low


def _string_value(node: Node, source: bytes) -> str | None:
    if node.type != "string":
        return None
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else ""


def _handler(arg_nodes: list[Node], source: bytes) -> tuple[str | None, int | None]:
    """The route handler → (name, line). By Express convention the handler is the
    **last** argument (any args between the path and it are middleware). Inline
    functions have no name, so only ``identifier`` / ``member_expression`` refs count."""
    if len(arg_nodes) < 2:
        return None, None
    last = arg_nodes[-1]
    if last.type in ("identifier", "member_expression"):
        return node_text(last, source), last.start_point[0] + 1
    return None, None


# The Apollo → Express adapter (``@apollo/server/express4``). ``app.use(path, expressMiddleware(server))``
# mounts the GraphQL transport endpoint — a real route, not a generic sub-router mount.
_APOLLO_MIDDLEWARE = "expressMiddleware"


def _has_apollo_middleware(arg_nodes: list[Node], source: bytes) -> bool:
    for a in arg_nodes:
        if a.type == "call_expression":
            callee = a.child_by_field_name("function")
            if callee is not None and node_text(callee, source).rsplit(".", 1)[-1] == _APOLLO_MIDDLEWARE:
                return True
    return False


def _resolve_str_identifier(name: str, root: Node, source: bytes) -> str | None:
    """Best-effort: resolve an identifier used as a mount path to a string literal — a
    param default (``graphqlPath = '/graphql'``) or a ``const graphqlPath = '/x'``."""
    found: str | None = None

    def walk(n: Node) -> None:
        nonlocal found
        if found is not None:
            return
        if n.type in ("required_parameter", "optional_parameter"):
            pat, val = n.child_by_field_name("pattern"), n.child_by_field_name("value")
            if pat is not None and node_text(pat, source) == name and val is not None and val.type == "string":
                found = _string_value(val, source)
                return
        if n.type == "variable_declarator":
            nm, val = n.child_by_field_name("name"), n.child_by_field_name("value")
            if nm is not None and node_text(nm, source) == name and val is not None and val.type == "string":
                found = _string_value(val, source)
                return
        for c in n.named_children:
            walk(c)

    walk(root)
    return found


def _classify(
    call: Node, source: bytes, root: Node
) -> tuple[str | None, str, str | None, int | None, str, str] | None:
    """→ (method, endpoint, handler, handlerLine, framework, routeKind), or None if not a route."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_expression":
        return None
    obj = fn.child_by_field_name("object")
    prop = fn.child_by_field_name("property")
    if obj is None or prop is None:
        return None
    method = node_text(prop, source)
    obj_text = node_text(obj, source)
    if not _is_router_obj(obj_text):
        return None

    args = call.child_by_field_name("arguments")
    arg_nodes = list(args.named_children) if args is not None else []
    path = _string_value(arg_nodes[0], source) if arg_nodes else None

    handler, handler_line = _handler(arg_nodes, source)

    if method in _HTTP_VERBS:
        # A verb call is a route only with a path arg + a handler arg — this rules out
        # the settings getter ``app.get('title')`` (single string arg, no handler).
        if path is not None and len(arg_nodes) >= 2:
            return method.upper(), path, handler, handler_line, "express", "route"
        return None
    if method == "use":
        # ``app.use(path, expressMiddleware(server))`` mounts the GraphQL endpoint (R3):
        # a POST route. The path is often a variable (``graphqlPath``) — resolve it, else
        # fall back to the ``/graphql`` convention.
        if _has_apollo_middleware(arg_nodes, source):
            arg0 = arg_nodes[0] if arg_nodes else None
            endpoint = path
            if endpoint is None and arg0 is not None and arg0.type == "identifier":
                endpoint = _resolve_str_identifier(node_text(arg0, source), root, source)
            return "POST", endpoint or "/graphql", None, None, "graphql", "route"
        if path is not None and path.startswith("/"):
            # ``app.use('/mount', router)`` mounts a sub-router; bare ``app.use(mw)`` is middleware.
            return None, path, handler, handler_line, "express", "mount"
        return None
    if method == "route" and path is not None:
        return None, path, None, None, "express", "route"
    return None


def _enclosing_statement(line: int, statements: list[Statement]) -> Statement | None:
    best: Statement | None = None
    best_span: int | None = None
    for s in statements:
        if s.startLine <= line <= s.endLine:
            span = s.endLine - s.startLine
            if best_span is None or span < best_span:
                best, best_span = s, span
    return best


def _owner_function(line: int, functions, fallback: str) -> str:
    best = None
    best_span: int | None = None
    for f in functions:
        if f.startLine <= line <= f.endLine:
            span = f.endLine - f.startLine
            if best_span is None or span < best_span:
                best, best_span = f, span
    return best.id if best is not None else fallback


def detect_express(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Enrich/add Express route statements on ``record``. Returns True if anything matched."""
    matched = False
    fid = file_id(path)
    seen = {s.id for s in record.statements}

    for call in _invocations(root):
        info = _classify(call, source, root)
        if info is None:
            continue
        method, endpoint, handler, handler_line, framework, route_kind = info
        line = call.start_point[0] + 1

        stmt = _enclosing_statement(line, record.statements)
        if stmt is not None:  # detection on the same span → enrich in place
            stmt.semanticType = "route"
            stmt.framework = framework
            stmt.routeKind = route_kind
            stmt.endpoint = endpoint
            if method:
                stmt.method = method
            if handler:
                stmt.handler = handler
                stmt.handlerLine = handler_line
        else:  # inside a handler/callback (base skips nested scopes) → add a statement
            new_id = disambiguate(statement_id(path, line, call.start_point[1]), seen)
            record.statements.append(Statement(
                id=new_id,
                parentId=_owner_function(line, record.functions, fid),
                nodeType=call.type,
                semanticType="route",
                text=first_line(node_text(call, source)),
                method=method,
                endpoint=endpoint,
                framework=framework,
                handler=handler,
                handlerLine=handler_line,
                routeKind=route_kind,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            ))
            seen.add(new_id)
        matched = True

    return matched
