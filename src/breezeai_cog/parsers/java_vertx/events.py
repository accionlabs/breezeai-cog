"""Vert.x event/route detection over the **Java** AST. Vert.x is call-based, so this
walks method invocations and maps them to event/messaging/route semantics:

  eventBus.send/publish/consumer → eventbus_send / eventbus_publish / eventbus_consumer
  vertx.setTimer/setPeriodic     → timer
  vertx.deployVerticle(...)      → verticle_deploy
  ServiceBinder…setAddress(...)  → eventbus_consumer (the service on that address)
  ServiceProxyBuilder…setAddress → eventbus_send     (a client of it; @ProxyGen: no record)
  router.get/post/…("/path")     → route

The shared decision table (which call → which semanticType) lives in
``parsers/vertx_common``; only the Java-grammar AST extraction (``_parts``) is here. The
Groovy counterpart is ``parsers/groovy_vertx/events.py``.

Per the capture contract, a detection sets ``semanticType`` on the **same span**: where the
base parser already captured the enclosing statement (top level of a method body) we enrich
it in place; for calls inside lambda handlers — which the base skips as a nested scope — we
add a statement parented to the enclosing function. Mutates ``record``.

**Delegation wrappers.** A common idiom hides the EventBus registration behind a thin
private helper, so the real addresses live at the call sites, not at the framework call::

    void start() {
        register("svc/formal",  new FormalHandler(this));   // ← the real consumers
        register("svc/submit",  new SubmitHandler(this));
    }
    private void register(String address, Handler<Message> h) {
        vertx.eventBus().registerHandler(address, h, ...);   // ← address is a parameter
    }

A single-pass detector sees only ``registerHandler(address, ...)`` — one consumer whose
endpoint is the unresolved variable ``"address"`` — and misses every real one. So before
classifying call sites we scan for such wrappers (``_wrapper_aliases``): a *private* method
whose first ``String`` parameter is forwarded *directly* (no transform) as the address of an
EventBus call. Its callers are then classified as that EventBus kind, and the wrapper's own
delegated call is suppressed (its address is a parameter — honest-null, not ``"address"``).
The direct-passthrough requirement keeps this tight: a method that rewrites the address is
not treated as a transparent alias. Detection is structural — the wrapper's *name* is
irrelevant (it need not be ``register``); only the shape is matched."""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...schemas import FileRecord, SemanticType
from ..additive import FactSpec, register_fact
from ..emits import Emit
from .java_syntax import (calls_and_creations, handler_class, imports_and_package,
                          invocations, java_string, local_value)
from .forwarders import forwarder_sites
from .legacy import http_client_calls, registration_tables
from .service_interfaces import service_methods
from ..treesitter import node_text
from ..handler_sites import handler_info, handler_location, locate_method, method_lines
from ..vertx_common import (
    vertx_emit,
    EVENTBUS,
    MOUNTED_FACTORIES,
    FactoryMount,
    factory_mount,
    resolve_mounted_factories,
    router_name,
    HANDLER_CALLS,
    HANDLER_SEMANTICS,
    MOUNT_SUB_ROUTER,
    SUB_ROUTER_CALL,
    SubRouterMount,
    classify_call,
    is_bus_receiver,
    join_path,
    render_address,
    router_vars,
    service_builder_vars,
    sub_router_bases,
)


_NOT_MOUNTED = object()


def _root_receiver(receiver: Node, source: bytes) -> str:
    """The router a route call is made on: ``router`` in ``router.get("/x")``. A chained call
    (``router.get("/x").handler(h)``) is the chain's first receiver."""
    node = receiver
    while node.type == "method_invocation" and (obj := node.child_by_field_name("object")):
        node = obj
    return node_text(node, source)


def _method_declarations(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "method_declaration":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _first_string_param(decl: Node, source: bytes) -> str | None:
    """Name of a method's first formal parameter when its type is ``String`` — the shape a
    Vert.x address must have; ``None`` otherwise. (A Vert.x address is always the *first*
    argument, so an alias wrapper forwards its own first parameter.)"""
    params = decl.child_by_field_name("parameters")
    if params is None:
        return None
    fp = next((c for c in params.named_children if c.type == "formal_parameter"), None)
    if fp is None:
        return None
    type_node = fp.child_by_field_name("type")
    name_node = fp.child_by_field_name("name")
    if type_node is None or name_node is None or node_text(type_node, source) != "String":
        return None
    return node_text(name_node, source)


#: Byte substrings that must both be present for a file to possibly contain an alias wrapper:
#: the ``private`` helper and some EventBus method name it forwards to. A cheap guard so the
#: extra declaration scan runs only on the rare file that could match (mirrors the Groovy
#: parser's ``b"RouteMatcher" in source`` gate).
_EVENTBUS_BYTES = tuple(m.encode() for m in EVENTBUS)


def _may_have_wrapper(source: bytes) -> bool:
    return b"private" in source and any(n in source for n in _EVENTBUS_BYTES)


def _wrapper_aliases(
    root: Node, source: bytes
) -> tuple[dict[str, SemanticType], set[tuple[int, int]]]:
    """Find EventBus **delegation wrappers** (see module docstring).

    Returns ``(aliases, suppress)``: ``aliases`` maps a wrapper method name to the EventBus
    ``SemanticType`` it delegates to (so its call sites classify as that kind); ``suppress``
    is the byte span of each wrapper's own delegated call — its address is the wrapper's
    parameter, so emitting it would fabricate an ``endpoint="address"`` edge (honest-null).

    Detection is purely structural (raw first-argument text vs the parameter name) — no
    address folding — so it stays cheap even when the byte guard admits many files."""
    aliases: dict[str, SemanticType] = {}
    suppress: set[tuple[int, int]] = set()
    for decl in _method_declarations(root):
        mods = next((c for c in decl.children if c.type == "modifiers"), None)
        if mods is None or "private" not in node_text(mods, source):
            continue
        param = _first_string_param(decl, source)
        name_node = decl.child_by_field_name("name")
        body = decl.child_by_field_name("body")
        if param is None or name_node is None or body is None:
            continue
        for call in invocations(body):
            name_n = call.child_by_field_name("name")
            method = node_text(name_n, source) if name_n is not None else ""
            if method not in EVENTBUS:
                continue
            obj_n = call.child_by_field_name("object")
            if obj_n is None or not is_bus_receiver(node_text(obj_n, source)):
                continue
            args = call.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            # A transparent alias forwards its parameter *unchanged* as the address: the raw
            # first argument must be exactly the parameter identifier (not `prefix + address`).
            if first is not None and node_text(first, source) == param:
                aliases[node_text(name_node, source)] = EVENTBUS[method]
                suppress.add((call.start_byte, call.end_byte))
                break
    return aliases, suppress


def _parts(
    call: Node, source: bytes, consts: dict[str, str]
) -> tuple[str, str | None, str | None, str]:
    """(method, folded-first-arg, raw-first-arg, receiver) for a Java ``method_invocation``.
    The Java grammar exposes ``object``/``name``/``arguments`` fields directly on the call.
    The address/path is the first argument; ``consts`` folds it when it is a String literal or
    a ``static final String`` constant (``registerHandler(ADDRESS_WEB, h)`` → its value)."""
    name_node = call.child_by_field_name("name")
    method = node_text(name_node, source) if name_node is not None else ""
    obj_node = call.child_by_field_name("object")
    obj = node_text(obj_node, source) if obj_node is not None else ""
    args = call.child_by_field_name("arguments")
    first_str = first_arg = None
    if args is not None and args.named_children:
        first_node = args.named_children[0]
        first_arg = node_text(first_node, source)
        first_str = render_address(first_node, source, consts, java_string)
    return method, first_str, first_arg, obj


def _classify(
    call: Node,
    source: bytes,
    consts: dict[str, str],
    aliases: dict[str, SemanticType],
    service_vars: dict[str, str] | None = None,
    routers: frozenset[str] = frozenset(),
) -> tuple[SemanticType, str | None, str | None, str | None] | None:
    """→ (semanticType, method, endpoint, routeKind) or None. Falls back to an alias-wrapper
    match (``aliases``) when the direct decision table does not classify the call — the
    address is the wrapper call's own first argument, folded like any other."""
    method, first_str, first_arg, obj = _parts(call, source, consts)
    info = classify_call(method, first_str, first_arg, obj, service_vars=service_vars,
                         routers=routers)
    if info is not None:
        return info
    semantic = aliases.get(method)
    if semantic is not None:  # an event-bus wrapper: resolved address or empty (Q15)
        return semantic, None, first_str if first_str and "{" not in first_str else None, None
    return None


def _handler_arg(call: Node, source: bytes) -> Node | None:
    """The handler argument of a route / consumer call: the second argument when the call takes
    the address and the handler (``consumer("a", this::h)``, 2.x ``rm.get("/x", h)``; a third,
    2.x ``registerHandler`` completion handler, is not it), else the argument of the last
    chained ``.handler(…)`` (``router.get("/x").handler(this::h)``)."""
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    found = named[1] if len(named) >= 2 else None
    node = call
    while (parent := node.parent) is not None and parent.type == "method_invocation" \
            and parent.child_by_field_name("object") == node:
        name = parent.child_by_field_name("name")
        if name is not None and node_text(name, source) in HANDLER_CALLS:
            pargs = parent.child_by_field_name("arguments")
            found = pargs.named_children[0] if pargs is not None and pargs.named_children else None
        node = parent
    return found


def _mount_arg(node: Node, source: bytes) -> str | None:
    """A mounted router given by name: ``apiRouter`` / ``this.apiRouter`` → ``apiRouter``."""
    if node.type == "identifier":
        return node_text(node, source)
    if node.type == "field_access":
        return router_name(node_text(node, source))
    return None


def _mount_calls(
    calls: list[Node], source: bytes, consts: dict[str, str]
) -> list[tuple[Node, Node, str, str | None, Node | None]]:
    """Every sub-router mount: (the mount call, the mounted argument, the parent router, the
    prefix, the ``route("/api/*")`` call it hangs off or None). ``router.route("/api/*")
    .subRouter(sub)`` (4.x) and ``router.mountSubRouter("/api", sub)`` (3.x)."""
    out: list[tuple[Node, Node, str, str | None, Node | None]] = []
    for call in calls:
        method, prefix, _, obj = _parts(call, source, consts)
        args = call.child_by_field_name("arguments")
        named = list(args.named_children) if args is not None else []
        if method == SUB_ROUTER_CALL and len(named) == 1:
            inner = call.child_by_field_name("object")
            if inner is None or inner.type != "method_invocation":
                continue
            inner_method, inner_path, inner_arg, inner_obj = _parts(inner, source, consts)
            if inner_method == "route" and inner_arg is not None:
                out.append((call, named[0], router_name(inner_obj), inner_path, inner))
        elif method == MOUNT_SUB_ROUTER and len(named) == 2:
            out.append((call, named[1], router_name(obj), prefix, None))
    return out


def _sub_router_mounts(
    calls: list[Node], source: bytes, consts: dict[str, str]
) -> tuple[list[SubRouterMount], set[tuple[int, int]]]:
    """Sub-router mounts by name in this file (a variable or a field), and the
    ``route("/api/*")`` calls every mount hangs off (the mount's path, not routes — whatever is
    mounted). A mounted factory call is collected repo-wide instead (:func:`_factory_mounts`)."""
    mounts: list[SubRouterMount] = []
    mount_routes: set[tuple[int, int]] = set()
    for _, arg, parent, prefix, inner in _mount_calls(calls, source, consts):
        if inner is not None:
            mount_routes.add((inner.start_byte, inner.end_byte))
        name = _mount_arg(arg, source)
        if name is not None:
            mounts.append((name, parent, prefix))
    return mounts, mount_routes


def _enclosing(node: Node, source: bytes) -> tuple[str | None, str | None]:
    """(class simple name, method name) a node is declared in."""
    method = cls = None
    parent = node.parent
    while parent is not None:
        if parent.type in ("method_declaration", "constructor_declaration") and method is None:
            name = parent.child_by_field_name("name")
            method = node_text(name, source) if name is not None else None
        elif parent.type in ("class_declaration", "enum_declaration", "record_declaration"):
            name = parent.child_by_field_name("name")
            cls = node_text(name, source) if name is not None else None
            break
        parent = parent.parent
    return cls, method


def _factory_routers(root: Node, source: bytes) -> dict[tuple[str, str], str]:
    """``(class, method)`` → the router variable a method creates (``Router.router(…)``) and
    returns: a router factory."""
    out: dict[tuple[str, str], str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(node.named_children)
        if node.type != "method_declaration":
            continue
        body = node.child_by_field_name("body")
        created: set[str] = set()
        returned: set[str] = set()
        inner = [body] if body is not None else []
        while inner:
            n = inner.pop()
            if n.type in ("lambda_expression", "class_body") and n is not body:
                continue  # a nested scope returns its own values
            inner.extend(n.named_children)
            if n.type == "variable_declarator":
                name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
                if name is not None and value is not None \
                        and node_text(value, source).replace(" ", "").startswith("Router.router("):
                    created.add(node_text(name, source))
            elif n.type == "return_statement":
                value = next(iter(n.named_children), None)
                if value is not None and value.type == "identifier":
                    returned.add(node_text(value, source))
        cls, method = _enclosing(body, source) if body is not None else (None, None)
        both = created & returned
        if cls and method and len(both) == 1:
            out[(cls, method)] = both.pop()
    return out


def _factory_key(call: Node, source: bytes) -> tuple[str, bool] | None:
    """A mounted factory call → (``Class.method``, True), or (``method``, False) when its class
    is not known from the call (``api.router()``). Not a call → None."""
    if call.type != "method_invocation":
        return None
    name = call.child_by_field_name("name")
    if name is None:
        return None
    method = node_text(name, source)
    obj = call.child_by_field_name("object")
    if obj is None:
        cls, _ = _enclosing(call, source)
        return (f"{cls}.{method}", True) if cls else (method, False)
    if obj.type == "identifier" and node_text(obj, source)[:1].isupper():  # a static call
        return f"{node_text(obj, source).rsplit('.', 1)[-1]}.{method}", True
    return method, False


def _factory_mounts(root: Node, source: bytes, rel: str, repo_root: Path | None) -> list[FactoryMount] | None:
    """The factory calls this file mounts, for the ``vertx.mounted-factories`` fact. A mount on
    a router that is itself mounted here has a prefix cog does not compose here → None."""
    calls = invocations(root)
    mounts = _mount_calls(calls, source, {})
    subs = {name for _, arg, _, _, _ in mounts if (name := _mount_arg(arg, source)) is not None}
    out: list[FactoryMount] = []
    for _, arg, parent, prefix, _ in mounts:
        key = _factory_key(arg, source)
        if key is not None:
            out.append((key[0], None if parent in subs else prefix, key[1]))
    return out or None


register_fact(FactSpec(
    name=MOUNTED_FACTORIES, language="java", gate=(b"ubRouter",),
    collect=_factory_mounts, resolve=resolve_mounted_factories,
))


def _registered_interface(call: Node, source: bytes) -> str | None:
    """``…setAddress(a).register(OrderService.class, impl)`` → ``OrderService`` (as written)."""
    node = call
    while (parent := node.parent) is not None and parent.type == "method_invocation" \
            and parent.child_by_field_name("object") == node:
        name = parent.child_by_field_name("name")
        if name is not None and node_text(name, source) in ("register", "registerLocal"):
            args = parent.child_by_field_name("arguments")
            first = args.named_children[0] if args is not None and args.named_children else None
            if first is not None and first.type == "class_literal" and first.named_children:
                return node_text(first.named_children[0], source)
            return None
        node = parent
    return None


def _registered_impl(call: Node, source: bytes) -> str | None:
    """``….register(OrderService.class, impl)`` → the implementing class: ``new OrderServiceImpl(…)``
    passed directly, or the local variable it was assigned to in the enclosing method."""
    node = call
    while (parent := node.parent) is not None and parent.type == "method_invocation" \
            and parent.child_by_field_name("object") == node:
        name = parent.child_by_field_name("name")
        if name is not None and node_text(name, source) in ("register", "registerLocal"):
            args = parent.child_by_field_name("arguments")
            impl = args.named_children[1] if args is not None and len(args.named_children) > 1 \
                else None
            if impl is not None and impl.type == "identifier":
                impl = local_value(impl, node_text(impl, source), source)
            if impl is not None and impl.type == "object_creation_expression":
                typ = impl.child_by_field_name("type")
                return node_text(typ, source) if typ is not None else None
            return None
        node = parent
    return None


def _qualified(type_name: str, root: Node, source: bytes) -> str:
    """A type as written in this file → its fully-qualified name: a single-type import that
    names it, else this file's package (a dotted name is taken as written)."""
    if "." in type_name:
        return type_name
    singles, _, package = imports_and_package(root, source)
    single = next((i for i in singles if i.endswith("." + type_name)), None)
    if single is not None:
        return single
    return f"{package}.{type_name}" if package else type_name


def _enclosing_types(node: Node, source: bytes) -> list[str]:
    """Names of the type declarations around ``node``, outermost first."""
    names: list[str] = []
    parent = node.parent
    while parent is not None:
        if parent.type in ("class_declaration", "interface_declaration", "enum_declaration",
                           "record_declaration"):
            nm = parent.child_by_field_name("name")
            if nm is not None:
                names.insert(0, node_text(nm, source))
        parent = parent.parent
    return names


def _service_fqn(type_name: str, call: Node, root: Node, source: bytes,
                 index: object | None) -> str:
    """The service interface a ``register(X.class, …)`` names → its fully-qualified name.

    A single-type import, or a name already known as written, resolves as in :func:`_qualified`.
    Otherwise the candidates are the name (its first segment, for ``Outer.Inner``) under each
    type enclosing the call, this file's package and each on-demand import; the one candidate
    known as a service interface (the ``vertx.service-interfaces`` fact) is kept. None or
    several known, or a wildcard match shadowed by a same-package type of that name (Java
    resolves the package's own type first), keep :func:`_qualified`'s answer: honest-null."""
    plain = _qualified(type_name, root, source)
    if service_methods(index, plain) or service_methods(index, type_name):
        return plain if service_methods(index, plain) else type_name
    head, _, rest = type_name.partition(".")
    singles, wildcards, package = imports_and_package(root, source)
    if any(i.endswith("." + head) for i in singles):
        single = next(i for i in singles if i.endswith("." + head))
        nested = f"{single}.{rest}" if rest else single
        return nested if service_methods(index, nested) else plain
    tail = f"{head}.{rest}" if rest else head
    prefix = f"{package}." if package else ""
    outers = _enclosing_types(call, source)
    scoped = [f"{prefix}{'.'.join(outers[:i])}.{tail}" for i in range(len(outers), 0, -1)]
    own = f"{prefix}{tail}"
    on_demand = [f"{w}.{tail}" for w in wildcards]
    known = list(dict.fromkeys(c for c in (*scoped, own, *on_demand) if service_methods(index, c)))
    if len(known) != 1:
        return plain
    if known[0] in on_demand and known[0] not in scoped and known[0] != own:
        fqcn = getattr(index, "fqcn", None) or {}
        if f"{prefix}{head}" in fqcn:  # a same-package type of that name shadows the import
            return plain
    return known[0]


def detect_vertx(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str] | None = None,
    index: object | None = None,
) -> list[Emit]:
    """This file's Vert.x routes, consumers, sends and timers, for :func:`~..emits.apply`.
    ``consts`` (``name → value``) folds symbolic addresses to their String value. ``index`` (the
    Java index) carries the ``vertx.service-interfaces`` fact: a ``ServiceBinder`` registration
    becomes one consumer per method of the registered interface."""
    found: list[Emit] = []
    consts = consts or {}

    aliases: dict[str, SemanticType] = {}
    suppress: set[tuple[int, int]] = set()
    if _may_have_wrapper(source):
        aliases, suppress = _wrapper_aliases(root, source)
    methods = method_lines(record.functions)
    calls, creations = calls_and_creations(root)
    mounts, mount_routes = (
        _sub_router_mounts(calls, source, consts) if b"ubRouter" in source else ([], set())
    )
    bases = sub_router_bases(mounts)
    # Routers a method of this file creates and returns: served where their factory is mounted.
    factories = _factory_routers(root, source) if b"Router.router" in source else {}
    service_vars = service_builder_vars(source) if b"setAddress" in source else {}
    routers = router_vars(source)
    # Vert.x 2 idioms (legacy.py): registration tables, address forwarders, HttpClient calls.
    handled: set[tuple[int, int]] = set()
    if b"org.vertx" in source:
        tables, replaced = registration_tables(calls, source, consts, index, path)
        suppress |= replaced
        sites, handled = forwarder_sites(root, calls, creations, source, path, record, consts,
                                         index, suppress)
        found.extend(tables)
        found.extend(sites)
        found.extend(http_client_calls(root, calls, source, record, consts))

    for call in calls:
        if (call.start_byte, call.end_byte) in suppress:  # wrapper plumbing → honest-null
            continue
        if (call.start_byte, call.end_byte) in handled:  # a call to a forwarder: recorded above
            continue
        if (call.start_byte, call.end_byte) in mount_routes:  # a sub-router's mount path: wiring
            continue
        info = _classify(call, source, consts, aliases, service_vars, routers)
        if info is None:
            continue
        semantic, method, endpoint, route_kind = info
        unknown_base = False
        receiver = call.child_by_field_name("object")
        recv = router_name(_root_receiver(receiver, source)) if receiver is not None else ""
        base: str | None | object = _NOT_MOUNTED
        if semantic == "route" and recv in bases:  # a sub-router's route: under its mount
            base = bases[recv]
        elif semantic == "route" and factories:
            cls, method_name = _enclosing(call, source)
            if cls and method_name and factories.get((cls, method_name)) == recv:
                mounted, prefix = factory_mount(index, cls, method_name)
                if mounted:
                    base = prefix
        if base is not _NOT_MOUNTED:
            if base is None or endpoint is None:
                unknown_base, endpoint = True, None
            else:
                endpoint = join_path(str(base), endpoint)
        handler, handler_line = (
            handler_info(_handler_arg(call, source), source, methods)
            if semantic in HANDLER_SEMANTICS else (None, None)
        )
        # A service registration: every method of the interface is a binding on the address
        # (the message's `action` header picks it) — one consumer per method, handler = method.
        handlers: list[str | None] = [handler]
        impl: str | None = None
        call_name = call.child_by_field_name("name")
        if semantic == "eventbus_consumer" and call_name is not None \
                and node_text(call_name, source) == "setAddress":
            iface = _registered_interface(call, source)
            names = service_methods(index, _service_fqn(iface, call, root, source, index)) \
                if iface else ()
            handlers = list(names) or [handler]
            # The code that runs is the implementing class's method (usually another file).
            impl = _registered_impl(call, source) if names else None
        elif handler is None and semantic in HANDLER_SEMANTICS:
            # `registerHandler(addr, new LoadHandler(eb))`: a Vert.x `Handler` has one method,
            # so the code that runs is that class's `handle` (usually another file).
            impl = handler_class(_handler_arg(call, source), source)
            if impl:
                handlers = ["handle"]

        def site(name: str | None) -> tuple[int | None, str | None]:
            return handler_location(locate_method(index, "java", impl, name) if impl else None,
                                    path)

        handler = handlers[0]
        if impl:
            handler_line, first_path = site(handler)
        else:
            first_path = None
        found.append(vertx_emit(call, semantic, method, endpoint, route_kind, handler,
                                handler_line, unknown_base=unknown_base,
                                handler_path=first_path))
        for extra in handlers[1:]:  # the others: their own records on the same span
            line, extra_path = site(extra)
            found.append(Emit(call, {
                "semanticType": semantic, "method": method, "endpoint": endpoint,
                "framework": "vertx", "routeKind": route_kind, "handler": extra,
                "handlerLine": line, "handlerPath": extra_path,
            }))

    return found
