"""Express route detection. Express is call-based — routes are registered
by calling an HTTP-verb method on an ``app`` / ``Router`` object:

  app.get('/users/:id', handler)   router.post('/users', handler)   → route
  app.use('/api', router)                                            → route (mount)
  app.route('/book')  (chained .get()/.post())                       → route (group)

Per the capture contract, a detection sets ``semanticType`` on the **same span**:
where the base parser already captured the enclosing statement (a top-level
``expression_statement``) we enrich it in place; for calls inside handler/callback
scopes — which the base skips as a nested scope — we add a statement parented to the
enclosing function. Mutates ``record`` (mirrors ``java_vertx``, the other call-based
detector)."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..statements_common import strip_leading_base, url_placeholder
from ..treesitter import first_line, node_text

# HTTP-verb methods that register a route handler on an app/router.
_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "options", "head", "all"}
_ROUTE_METHODS = _HTTP_VERBS | {"use", "route"}


def _invocations(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "call_expression":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


_SCOPE_TYPES = {
    "arrow_function", "class_body", "function_declaration", "function_expression",
    "method_definition", "program", "statement_block",
}


def _scope_path(node: Node) -> list[Node]:
    scopes: list[Node] = []
    parent = node.parent
    while parent is not None:
        if parent.type in _SCOPE_TYPES:
            scopes.append(parent)
        parent = parent.parent
    scopes.reverse()
    return scopes


def _router_bindings(
    root: Node, source: bytes
) -> dict[str, list[tuple[int, list[Node], Node | None, bool]]]:
    """Index declarations and direct assignments by name and lexical scope."""
    bindings: dict[str, list[tuple[int, list[Node], Node | None, bool]]] = {}

    def walk(node: Node) -> None:
        if node.type == "variable_declarator":
            binding = node.child_by_field_name("name")
            if binding is not None and binding.type == "identifier":
                name = node_text(binding, source)
                bindings.setdefault(name, []).append((
                    node.start_byte,
                    _scope_path(node),
                    node.child_by_field_name("value"),
                    False,
                ))
        elif node.type == "assignment_expression":
            binding = node.child_by_field_name("left")
            value = node.child_by_field_name("right")
            if binding is not None and binding.type == "identifier":
                operator = (
                    source[binding.end_byte:value.start_byte].strip()
                    if value is not None
                    else b""
                )
                if operator == b"=":
                    name = node_text(binding, source)
                    bindings.setdefault(name, []).append((
                        node.start_byte,
                        _scope_path(node),
                        value,
                        True,
                    ))
        for child in node.named_children:
            walk(child)

    walk(root)
    return bindings


def _router_initializer(
    name: str,
    before_byte: int,
    bindings: dict[str, list[tuple[int, list[Node], Node | None, bool]]],
    scope: list[Node],
) -> tuple[bool, Node | None, bool]:
    """Resolve a visible declaration and its latest preceding direct assignment."""
    candidates: list[tuple[int, list[Node], Node | None, bool]] = []
    for entry in bindings.get(name, ()):
        start_byte, binding_scope, _, is_assignment = entry
        if (
            not is_assignment
            and len(binding_scope) <= len(scope)
            and all(left == right for left, right in zip(binding_scope, scope))
        ):
            candidates.append(entry)
    if not candidates:
        return False, None, False

    visible_depth = max(len(entry[1]) for entry in candidates)
    visible = [entry for entry in candidates if len(entry[1]) == visible_depth]
    preceding = [entry for entry in visible if entry[0] < before_byte]
    declaration = (
        max(preceding, key=lambda entry: entry[0])
        if preceding
        else min(visible, key=lambda entry: entry[0])
    )
    start_byte, binding_scope, initializer, _ = declaration
    if start_byte >= before_byte:
        return True, None, False

    latest_assignment: tuple[int, Node] | None = None
    for assigned_byte, assignment_scope, value, is_assignment in bindings.get(name, ()):
        if (
            is_assignment
            and start_byte < assigned_byte < before_byte
            and len(binding_scope) <= len(assignment_scope) <= len(scope)
            and all(left == right for left, right in zip(binding_scope, assignment_scope))
            and all(left == right for left, right in zip(assignment_scope, scope))
            and value is not None
            and (latest_assignment is None or assigned_byte > latest_assignment[0])
        ):
            latest_assignment = (assigned_byte, value)
    if latest_assignment is not None:
        return True, latest_assignment[1], True
    return True, initializer, initializer is not None


def _unwrap_expression(node: Node) -> Node:
    """Remove transparent TypeScript and parenthesis wrappers around an expression."""
    while node.type in {
        "as_expression", "non_null_expression", "parenthesized_expression",
    }:
        expression = node.child_by_field_name("expression")
        if expression is None and node.named_children:
            expression = node.named_children[0]
        if expression is None:
            break
        node = expression
    return node


def _is_express_constructor(node: Node | None, source: bytes) -> bool | None:
    """True for known Express constructors, False for known non-routers, else unknown."""
    if node is None:
        return None
    node = _unwrap_expression(node)
    if node.type in {
        "object", "array", "string", "template_string", "number", "true", "false",
        "null", "regex", "arrow_function", "function_expression", "class",
    }:
        return False
    if node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    if fn is None:
        return None
    fn = _unwrap_expression(fn)
    if fn.type == "identifier":
        return True if node_text(fn, source) in {"express", "Router"} else None
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        obj = fn.child_by_field_name("object")
        if prop is None or obj is None or node_text(prop, source) != "Router":
            return None
        obj = _unwrap_expression(obj)
        if obj.type == "identifier" and node_text(obj, source) == "express":
            return True
        if obj.type == "call_expression":
            require_fn = obj.child_by_field_name("function")
            require_args = obj.child_by_field_name("arguments")
            args = list(require_args.named_children) if require_args is not None else []
            if (
                require_fn is not None
                and require_fn.type == "identifier"
                and node_text(require_fn, source) == "require"
                and len(args) == 1
                and _string_value(args[0], source) == "express"
            ):
                return True
        return None
    return None


def _is_router_obj(
    obj: Node,
    source: bytes,
    router_bindings: dict[str, list[tuple[int, list[Node], Node | None, bool]]],
    before_byte: int,
) -> bool:
    """Accept known Express constructors, unresolved router names, and reject local plain objects."""
    obj_text = node_text(obj, source)
    if obj.type == "call_expression":
        return _is_express_constructor(obj, source) is True

    name = obj_text.rsplit(".", 1)[-1].strip()
    declared, initializer, initialized = _router_initializer(
        name, before_byte, router_bindings, _scope_path(obj)
    )
    if declared:
        router = _is_express_constructor(initializer, source)
        if router is not None:
            return router
        if not initialized:
            return False

    low = name.lower()
    return low in {"app", "router", "server", "api", "route"} or low.endswith(
        ("router", "app")
    )


def _string_value(node: Node, source: bytes) -> str | None:
    if node.type != "string":
        return None
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else ""


def _template_value(node: Node, source: bytes) -> str:
    """Render a ``template_string`` path to a route path, turning each ``${expr}`` into a
    ``{name}`` placeholder (``/sitemaps/${key}.txt`` → ``/sitemaps/{key}.txt``). A leading
    interpolated base/host segment is dropped so the path matches inbound routes."""
    parts: list[str] = []
    for c in node.named_children:
        if c.type == "string_fragment":
            parts.append(node_text(c, source))
        elif c.type == "template_substitution":
            expr = c.named_children[0] if c.named_children else None
            parts.append(url_placeholder(node_text(expr, source)) if expr is not None else "{param}")
    return strip_leading_base("".join(parts))


def _path_value(node: Node, source: bytes) -> str | None:
    """Route-path argument as a string: a plain string literal verbatim, or a template
    literal rendered with ``{param}`` placeholders. Unlike ``_string_value`` (used for
    resolving identifier constants), this accepts the dynamic template-literal form."""
    if node.type == "template_string":
        return _template_value(node, source)
    return _string_value(node, source)


def _chained_route_path(
    call: Node,
    source: bytes,
    router_bindings: dict[str, list[tuple[int, list[Node], Node | None, bool]]],
) -> str | None:
    """Find the path of the ``.route(path)`` at the base of a chained verb call."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_expression":
        return None
    obj = fn.child_by_field_name("object")
    prop = fn.child_by_field_name("property")
    if obj is None or prop is None:
        return None

    method = node_text(prop, source)
    if method == "route":
        if not _is_router_obj(obj, source, router_bindings, call.start_byte):
            return None
        args = call.child_by_field_name("arguments")
        route_args = list(args.named_children) if args is not None else []
        return _path_value(route_args[0], source) if route_args else None
    if method in _HTTP_VERBS and obj.type == "call_expression":
        return _chained_route_path(obj, source, router_bindings)
    return None


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


def _mw_name(node: Node, source: bytes) -> str | None:
    """A middleware/handler arg's callable name — ``requireAuth`` from ``requireAuth()``,
    ``Auth.check`` from the member call, ``rateLimit`` from a bare reference. An inline
    (arrow/function) middleware has no name → None (honest-null, no fabricated name)."""
    n = node.child_by_field_name("function") if node.type == "call_expression" else node
    if n is not None and n.type in ("identifier", "member_expression"):
        return node_text(n, source)
    return None


def _guard_names(nodes: list[Node], source: bytes) -> list[str] | None:
    """Named middleware in a route/mount chain → guard names, in source order. Unnamed inline
    middleware are skipped. Empty → None (so the field stays unset rather than an empty list)."""
    names = [nm for n in nodes if (nm := _mw_name(n, source)) is not None]
    return names or None


def _is_internal_factory_call(node: Node, source: bytes, bindings: dict[str, str]) -> bool:
    """Whether ``node`` is a bare ``factory()`` call on an **internally-imported** name — i.e. a
    mounted sub-router, not a guard. A member call (``Auth.check()``) or an externally-imported
    ``cors()`` is not in ``bindings`` (which holds only in-repo imports), so is treated as a
    guard — precision-first, mirroring the honest-null bias elsewhere."""
    if node.type != "call_expression":
        return False
    fn = node.child_by_field_name("function")
    return fn is not None and fn.type == "identifier" and node_text(fn, source) in bindings


def _mount_parts(
    chain: list[Node], source: bytes, bindings: dict[str, str]
) -> tuple[str | None, int | None, list[str] | None]:
    """Split a mount's arg chain (everything after the path) into (handler, handlerLine, guards):
    the mounted sub-router — the single bare internally-imported ``factory()`` call — is the
    handler, and the surrounding middleware are guards. When the router is not cleanly
    identifiable (0 or >1 such calls), fall back to the Express default (last arg is the handler,
    the rest are guards) so the result is never worse than before."""
    routers = [n for n in chain if _is_internal_factory_call(n, source, bindings)]
    if len(routers) == 1:
        router = routers[0]
        fn = router.child_by_field_name("function")
        handler = node_text(fn, source) if fn is not None else None
        guards = _guard_names([n for n in chain if n is not router], source)
        return handler, router.start_point[0] + 1, guards
    if not chain:
        return None, None, None
    name = _mw_name(chain[-1], source)  # ambiguous → Express default: last arg is the handler
    line = chain[-1].start_point[0] + 1 if name is not None else None
    return name, line, _guard_names(chain[:-1], source)


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
    call: Node,
    source: bytes,
    root: Node,
    router_bindings: dict[str, list[tuple[int, list[Node], Node | None, bool]]],
    bindings: dict[str, str],
) -> tuple[str | None, str, str | None, int | None, list[str] | None, str, str, bool] | None:
    """Return route details, chained-call status, or None if the call is not a route.

    ``guards`` are the named route/mount middleware (auth/interceptor stack); ``bindings``
    (in-repo imported name → file) lets a mount tell its sub-router from its guard middleware.
    """
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_expression":
        return None
    obj = fn.child_by_field_name("object")
    prop = fn.child_by_field_name("property")
    if obj is None or prop is None:
        return None
    method = node_text(prop, source)
    if method not in _ROUTE_METHODS:
        return None
    if method == "route":
        return None
    args = call.child_by_field_name("arguments")
    arg_nodes = list(args.named_children) if args is not None else []
    chained_path = (
        _chained_route_path(call, source, router_bindings)
        if method in _HTTP_VERBS
        else None
    )
    if method in _HTTP_VERBS and chained_path is not None and arg_nodes:
        last = arg_nodes[-1]
        handler = node_text(last, source) if last.type in (
            "identifier", "member_expression"
        ) else None
        handler_line = last.start_point[0] + 1 if handler is not None else None
        guards = _guard_names(arg_nodes[:-1], source)
        route_method = "ANY" if method == "all" else method.upper()
        return (
            route_method, chained_path, handler, handler_line, guards, "express", "route", True
        )

    if not _is_router_obj(obj, source, router_bindings, call.start_byte):
        return None

    path = _path_value(arg_nodes[0], source) if arg_nodes else None
    chain = arg_nodes[1:]  # everything after the path — the middleware + handler/router stack

    if method in _HTTP_VERBS:
        # A verb call is a route only with a path arg + a handler arg — this rules out
        # the settings getter ``app.get('title')`` (single string arg, no handler). The last
        # arg is the terminal handler; the middleware before it are guards.
        if path is not None and len(arg_nodes) >= 2:
            handler, handler_line = _handler(arg_nodes, source)
            guards = _guard_names(chain[:-1], source)
            route_method = "ANY" if method == "all" else method.upper()
            return route_method, path, handler, handler_line, guards, "express", "route", False
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
            return "POST", endpoint or "/graphql", None, None, None, "graphql", "route", False
        if path is not None and path.startswith("/"):
            # ``app.use('/mount', ...guards, subRouter, ...guards)`` mounts a sub-router: the
            # mounted router is the handler, the surrounding middleware are guards. Bare
            # ``app.use(mw)`` (no leading path) is middleware, not a mount.
            handler, handler_line, guards = _mount_parts(chain, source, bindings)
            return "ANY", path, handler, handler_line, guards, "express", "mount", False
        return None
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


def _has_express(source: bytes) -> bool:
    """Cheap correctness gate: the file imports ``express`` (either quote style). The
    receiver-name fallback in :func:`_is_router_obj` is only safe on files that actually use
    Express, so this guard — not selection — bounds it now that detection runs additively for
    every TS file (see TypeScriptParser.extract)."""
    return (b"'express'" in source or b'"express"' in source
            or b"expressMiddleware" in source)  # Apollo → Express adapter mount


def _join_base(base: str, local: str) -> str:
    """Join a mount base with a router-local route path — single slash, empties ignored
    (``/users`` + ``/:id`` → ``/users/:id``; ``/`` + ``/`` → ``/``). Mirrors the Angular
    detector's ``_join`` so composed prefixes match within-file composition."""
    parts = [p.strip("/") for p in (base, local) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _apply_base(
    base: str | None, framework: str, route_kind: str, endpoint: str | None
) -> str | None:
    """Prefix a router-local Express route with the base its factory is mounted at, when the
    repo index resolved one for this file. Only express ``route`` statements are joined — a
    ``mount`` keeps its own base, and other frameworks are untouched. No resolved mount (or a
    dynamic endpoint) → endpoint unchanged (honest-null; today's behavior)."""
    if base is None or endpoint is None or framework != "express" or route_kind != "route":
        return endpoint
    return _join_base(base, endpoint)


def detect_express(
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
    index: object | None = None,
    bindings: dict[str, str] | None = None,
) -> bool:
    """Enrich/add Express route statements on ``record``. Returns True if anything matched.

    Additive: invoked from ``TypeScriptParser.extract`` for every TS file (whatever parser
    owns it), self-guarded by :func:`_has_express`, and enriching the base parser's existing
    statement in place — so it captures Express routes even in files owned by another
    framework (Angular SSR, NestJS) without duplicating or displacing that owner.

    ``index`` is the repo-wide ``TsAliasIndex`` (or None). When this file is a router factory
    mounted elsewhere (``app.use('/users', usersRouter())``), ``index.express_mounts`` maps its
    path to the base it is served under, so a router-local ``route.get('/:id')`` is recorded at
    its real URL ``/users/:id`` instead of the bare ``/:id``.

    ``bindings`` (in-repo imported name → file) lets a mount separate its sub-router from its
    guard middleware, so route-level auth (``requireAuth()``) lands in ``guards`` rather than
    being dropped or mistaken for the handler."""
    if not _has_express(source):
        return False
    matched = False
    fid = file_id(path)
    seen = {s.id for s in record.statements}
    binds = bindings or {}
    router_bindings = _router_bindings(root, source)
    # The base this file's routes are served under, if it is a factory mounted elsewhere.
    mounts = getattr(index, "express_mounts", None)
    mount_base = mounts.get(path) if isinstance(mounts, dict) else None

    for call in _invocations(root):
        info = _classify(call, source, root, router_bindings, binds)
        if info is None:
            continue
        method, endpoint, handler, handler_line, guards, framework, route_kind, chained = info
        served = _apply_base(mount_base, framework, route_kind, endpoint)
        # Honest-null: assert auth only when a route-level guard is present. Absence is
        # "unknown" (None), not "open" — app-level middleware may still protect the route.
        auth_required = True if guards else None
        line = call.start_point[0] + 1

        stmt = None if chained else _enclosing_statement(line, record.statements)
        if stmt is not None:  # detection on the same span → enrich in place
            stmt.semanticType = "route"
            stmt.framework = framework
            stmt.routeKind = route_kind
            stmt.endpoint = served
            if method:
                stmt.method = method
            if handler:
                stmt.handler = handler
                stmt.handlerLine = handler_line
            if guards:
                stmt.guards = guards
                stmt.authRequired = True
        else:  # inside a handler/callback (base skips nested scopes) → add a statement
            new_id = disambiguate(statement_id(path, line, call.start_point[1]), seen)
            record.statements.append(Statement(
                id=new_id,
                parentId=_owner_function(line, record.functions, fid),
                nodeType=call.type,
                semanticType="route",
                text=first_line(node_text(call, source)),
                method=method,
                endpoint=served,
                framework=framework,
                handler=handler,
                handlerLine=handler_line,
                guards=guards,
                authRequired=auth_required,
                routeKind=route_kind,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            ))
            seen.add(new_id)
        matched = True

    return matched
