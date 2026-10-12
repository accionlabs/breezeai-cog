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
from ..additive import DetectContext, Detector, register_detector
from ..enclosing import Enclosing
from ..treesitter import first_line, node_text
from ..typescript.imports import resolve_const_value, router_locals

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


def _path_value(node: Node, source: bytes, consts: dict[str, str | None]) -> str | None:
    """Route-path argument as a string: a plain string literal verbatim, or a template literal
    whose every ``${…}`` resolves to a constant (``consts``: the repo's string constants). An
    unresolved part makes the whole path unknown → None — never a ``{name}`` placeholder or a
    path with its base dropped (``${BASE}/users`` is not ``/users``)."""
    if node.type == "template_string":
        return resolve_const_value(node, source, consts)
    return _string_value(node, source)


_FUNCTIONS = ("arrow_function", "function_expression", "function")


def _handler(
    arg_nodes: list[Node], source: bytes, decl_lines: dict[str, int | None]
) -> tuple[str | None, int | None]:
    """The route handler → (name, handlerLine). By Express convention the handler is the
    **last** argument (any args between the path and it are middleware).

    - A named reference (``listUsers`` / ``UsersController.list``): its name, and the line of
      its declaration when it is a top-level function of this file (``decl_lines``); a handler
      declared elsewhere keeps its name with no line (never the line of the reference).
    - An inline function: no name; ``handlerLine`` is where the function starts.
    - A wrapper call (``asyncHandler(async (req, res) => …)``): wrappers are not handlers — the
      handler is the wrapper's last argument, unwrapped the same way. A call with no argument to
      unwrap (``controller.handle()``) identifies no handler → (None, None)."""
    if len(arg_nodes) < 2:
        return None, None
    last: Node | None = arg_nodes[-1]
    while last is not None and last.type == "call_expression":
        args = last.child_by_field_name("arguments")
        inner = list(args.named_children) if args is not None else []
        last = inner[-1] if inner else None
    if last is None:
        return None, None
    if last.type in _FUNCTIONS:
        return None, last.start_point[0] + 1
    if last.type in ("identifier", "member_expression"):
        name = node_text(last, source)
        return name, decl_lines.get(name)
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
    call: Node, source: bytes, root: Node, bindings: dict[str, str],
    routers: frozenset[str] = frozenset(), decl_lines: dict[str, int | None] | None = None,
    consts: dict[str, str | None] | None = None,
) -> tuple[str | None, str | None, str | None, int | None, list[str] | None, str, str] | None:
    """→ (method, endpoint, handler, handlerLine, guards, framework, routeKind), or None if not
    a route. ``guards`` are the named route/mount middleware (auth/interceptor stack); ``bindings``
    (in-repo imported name → file) lets a mount tell its sub-router from its guard middleware.
    ``routers`` are variables bound to ``Router()`` in this file — receivers whatever their name."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_expression":
        return None
    obj = fn.child_by_field_name("object")
    prop = fn.child_by_field_name("property")
    if obj is None or prop is None:
        return None
    method = node_text(prop, source)
    # ``app.route('/book').get(h1).post(h2)``: each verb call is a binding at the route's path;
    # ``route(path)`` itself only adds the path (wiring — no record).
    route_call = _route_root(obj, source) if method in _HTTP_VERBS else None
    if route_call is not None:
        return _classify_route_verb(call, route_call, method, source, routers, decl_lines, consts)
    obj_text = node_text(obj, source)
    if not _is_router_obj(obj_text) and obj_text not in routers:
        return None

    args = call.child_by_field_name("arguments")
    arg_nodes = list(args.named_children) if args is not None else []
    path = _path_value(arg_nodes[0], source, consts or {}) if arg_nodes else None
    # A template-literal path is a path even when it does not resolve: the route is recorded
    # with an empty endpoint rather than dropped.
    has_path = path is not None or (bool(arg_nodes) and arg_nodes[0].type == "template_string")
    chain = arg_nodes[1:]  # everything after the path — the middleware + handler/router stack

    if method in _HTTP_VERBS:
        # A verb call is a route only with a path arg + a handler arg — this rules out
        # the settings getter ``app.get('title')`` (single string arg, no handler). The last
        # arg is the terminal handler; the middleware before it are guards.
        if has_path and len(arg_nodes) >= 2:
            handler, handler_line = _handler(arg_nodes, source, decl_lines or {})
            guards = _guard_names(chain[:-1], source)
            return method.upper(), path, handler, handler_line, guards, "express", "route"
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
            return "POST", endpoint or "/graphql", None, None, None, "graphql", "route"
        if has_path and (path is None or path.startswith("/")):
            # ``app.use('/mount', ...guards, subRouter, ...guards)`` mounts a sub-router: the
            # mounted router is the handler, the surrounding middleware are guards. Bare
            # ``app.use(mw)`` (no leading path) is middleware, not a mount.
            handler, handler_line, guards = _mount_parts(chain, source, bindings)
            return None, path, handler, handler_line, guards, "express", "mount"
        return None
    return None  # includes ``route(path)``: wiring for its chained verbs


def _route_root(obj: Node | None, source: bytes) -> Node | None:
    """The ``X.route(path)`` call at the root of a verb chain receiver
    (``app.route('/book')`` / ``app.route('/book').get(h1)``), else None."""
    while obj is not None and obj.type == "call_expression":
        fn = obj.child_by_field_name("function")
        prop = fn.child_by_field_name("property") if fn is not None and fn.type == "member_expression" else None
        if fn is None or prop is None:
            return None
        name = node_text(prop, source)
        if name == "route":
            return obj
        if name not in _HTTP_VERBS:
            return None
        obj = fn.child_by_field_name("object")
    return None


def _classify_route_verb(
    call: Node, route_call: Node, method: str, source: bytes, routers: frozenset[str],
    decl_lines: dict[str, int | None] | None, consts: dict[str, str | None] | None,
) -> tuple[str | None, str | None, str | None, int | None, list[str] | None, str, str] | None:
    """One verb of an ``X.route(path).get(...)`` chain → a route at ``path`` with the verb's
    handler (last argument) and guards (the arguments before it)."""
    rfn = route_call.child_by_field_name("function")
    robj = rfn.child_by_field_name("object") if rfn is not None else None
    if robj is None:
        return None
    robj_text = node_text(robj, source)
    if not _is_router_obj(robj_text) and robj_text not in routers:
        return None
    rargs = route_call.child_by_field_name("arguments")
    first = rargs.named_children[0] if rargs is not None and rargs.named_children else None
    if first is None:
        return None
    path = _path_value(first, source, consts or {})
    if path is None and first.type != "template_string":
        return None
    vargs = call.child_by_field_name("arguments")
    verb_args = list(vargs.named_children) if vargs is not None else []
    if not verb_args:
        return None
    handler, handler_line = _handler([first, *verb_args], source, decl_lines or {})
    return method.upper(), path, handler, handler_line, _guard_names(verb_args[:-1], source), \
        "express", "route"


def _receiver(call: Node, source: bytes) -> str:
    """The object a method call is made on (``matchRouter`` in ``matchRouter.get(...)``; for a
    ``router.route(path).get(...)`` chain, the router)."""
    fn = call.child_by_field_name("function")
    obj = fn.child_by_field_name("object") if fn is not None else None
    route_call = _route_root(obj, source)
    if route_call is not None:
        rfn = route_call.child_by_field_name("function")
        obj = rfn.child_by_field_name("object") if rfn is not None else None
    return node_text(obj, source) if obj is not None else ""


def _has_express(source: bytes) -> bool:
    """Cheap correctness gate: the file imports ``express`` (either quote style). The
    ``app``/``router``/``route`` receiver heuristic in ``_is_router_obj`` is only safe on
    files that actually use Express, so this guard — not selection — bounds it now that
    detection runs additively for every TS file (see TypeScriptParser.extract)."""
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


def _receiver_bases(
    root: Node, source: bytes, file_base: str | None, imported: dict[str, str]
) -> dict[str, str]:
    """Router variable → the base its routes are served under, for routers in this file.

    ``imported`` holds the bases the repo index resolved for routers this file exports and
    another file mounts (``app.use('/matches', matchRouter)``). Same-file mounts
    (``const r = Router(); app.use('/api', r)``) are composed on top: a router mounted on another
    router inherits that router's base; one mounted on the app (or an unmounted router) starts
    from ``file_base`` (a factory file's base) or ``/``. A router mounted at >1 distinct base, or
    in a cycle, gets no entry (honest-null: its routes keep their own path)."""
    routers = router_locals(root, source) if b"Router" in source else set()
    local: dict[str, tuple[str, str] | None] = {}  # router → (receiver it is mounted on, path)
    for call in _invocations(root) if routers else []:
        fn = call.child_by_field_name("function")
        is_member = fn is not None and fn.type == "member_expression"
        prop = fn.child_by_field_name("property") if fn is not None and is_member else None
        if prop is None or node_text(prop, source) != "use":
            continue
        args = call.child_by_field_name("arguments")
        arg_nodes = list(args.named_children) if args is not None else []
        path = _string_value(arg_nodes[0], source) if arg_nodes else None
        if path is None or not path.startswith("/"):
            continue
        receiver = _receiver(call, source)
        for a in arg_nodes[1:]:
            name = node_text(a, source)
            if a.type == "identifier" and name in routers:
                entry = (receiver, path)
                local[name] = entry if local.get(name, entry) == entry else None

    out: dict[str, str] = dict(imported)

    def resolve(router: str, seen: frozenset[str]) -> str | None:
        if router in out:
            return out[router]
        entry = local.get(router)
        if entry is None or router in seen:  # ambiguous, or a mount cycle
            return None
        receiver, path = entry
        if receiver in local:  # mounted on a router that is itself mounted in this file
            parent = resolve(receiver, seen | {router})
            return _join_base(parent, path) if parent is not None else None
        return _join_base(imported.get(receiver) or file_base or "/", path)

    for router in local:
        if (base := resolve(router, frozenset())) is not None:
            out[router] = base
    return out


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
    enclosing = Enclosing(record)
    binds = bindings or {}
    # The base this file's routes are served under, if it is a factory mounted elsewhere.
    mounts = getattr(index, "express_mounts", None)
    mount_base = mounts.get(path) if isinstance(mounts, dict) else None
    # Per router variable: routers this file exports that another file mounts, and routers
    # mounted in this file.
    instance = getattr(index, "express_router_mounts", None)
    imported = instance.get(path, {}) if isinstance(instance, dict) else {}
    receiver_base = _receiver_bases(root, source, mount_base, imported)
    routers = frozenset(router_locals(root, source)) if b"Router" in source else frozenset()
    index_consts = getattr(index, "const_values", None)
    consts: dict[str, str | None] = index_consts if isinstance(index_consts, dict) else {}
    # Top-level functions of this file → declaration line; a name declared twice → None.
    decl_lines: dict[str, int | None] = {}
    for f in record.functions:
        if f.parentId == fid:
            decl_lines[f.name] = f.startLine if f.name not in decl_lines else None

    enriched: set[str] = set()  # statements enriched by this run
    for call in _invocations(root):
        info = _classify(call, source, root, binds, routers, decl_lines, consts)
        if info is None:
            continue
        method, endpoint, handler, handler_line, guards, framework, route_kind = info
        served = _apply_base(
            receiver_base.get(_receiver(call, source), mount_base), framework, route_kind, endpoint
        )
        # Honest-null: assert auth only when a route-level guard is present. Absence is
        # "unknown" (None), not "open" — app-level middleware may still protect the route.
        auth_required = True if guards else None
        line = call.start_point[0] + 1

        stmt = enclosing.statement(line)
        if stmt is not None and stmt.id in enriched:
            stmt = None  # a second route in one statement (route(path).get(a).post(b)) → own record
        if stmt is not None:  # detection on the same span → enrich in place
            enriched.add(stmt.id)
            stmt.semanticType = "route"
            stmt.framework = framework
            stmt.routeKind = route_kind
            stmt.endpoint = served
            if method:
                stmt.method = method
            if handler:
                stmt.handler = handler
            if handler_line:
                stmt.handlerLine = handler_line
            if guards:
                stmt.guards = guards
                stmt.authRequired = True
        else:  # inside a handler/callback (base skips nested scopes) → add a statement
            new_id = disambiguate(statement_id(path, line, call.start_point[1]), seen)
            record.statements.append(Statement(
                id=new_id,
                parentId=enclosing.function_id(line, fid),
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
            enriched.add(new_id)
        matched = True

    return matched


def _run(dc: DetectContext) -> str | None:
    matched = detect_express(
        dc.root, dc.source, dc.path, dc.record, dc.ctx.resolution_index, dc.bindings
    )
    return "express" if matched else None


register_detector(Detector(
    name="express", language="typescript", order=10, run=_run,
    skip_fixtures=True, frameworks=("express",),
))
