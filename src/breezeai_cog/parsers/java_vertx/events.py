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

from tree_sitter import Node

from ...schemas import FileRecord, SemanticType
from ..emits import Emit
from ..method_lines import locate_method
from .service_interfaces import service_methods
from ..treesitter import node_text
from ..vertx_common import (
    vertx_emit,
    EVENTBUS,
    HANDLER_CALLS,
    HANDLER_SEMANTICS,
    MOUNT_SUB_ROUTER,
    SUB_ROUTER_CALL,
    SubRouterMount,
    classify_call,
    handler_info,
    is_bus_receiver,
    join_path,
    method_lines,
    render_address,
    service_builder_vars,
    sub_router_bases,
)


def _java_string(node: Node, source: bytes) -> str | None:
    """A Java ``string_literal`` node → its text value (Java has no string interpolation)."""
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else node_text(node, source).strip('"')


def _invocations(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "method_invocation":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


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
        for call in _invocations(body):
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
        first_str = render_address(first_node, source, consts, _java_string)
    return method, first_str, first_arg, obj


def _classify(
    call: Node,
    source: bytes,
    consts: dict[str, str],
    aliases: dict[str, SemanticType],
    service_vars: dict[str, str] | None = None,
) -> tuple[SemanticType, str | None, str | None, str | None] | None:
    """→ (semanticType, method, endpoint, routeKind) or None. Falls back to an alias-wrapper
    match (``aliases``) when the direct decision table does not classify the call — the
    address is the wrapper call's own first argument, folded like any other."""
    method, first_str, first_arg, obj = _parts(call, source, consts)
    info = classify_call(method, first_str, first_arg, obj, service_vars=service_vars)
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


def _sub_router_mounts(
    calls: list[Node], source: bytes, consts: dict[str, str]
) -> tuple[list[SubRouterMount], set[tuple[int, int]]]:
    """Sub-router mounts in this file, and the ``route("/api/*")`` calls they hang off (those are
    the mount's path, not routes). ``router.route("/api/*").subRouter(sub)`` and
    ``router.mountSubRouter("/api", sub)``; ``sub`` must be a plain variable."""
    mounts: list[SubRouterMount] = []
    mount_routes: set[tuple[int, int]] = set()
    for call in calls:
        method, prefix, _, obj = _parts(call, source, consts)
        args = call.child_by_field_name("arguments")
        named = list(args.named_children) if args is not None else []
        if method == SUB_ROUTER_CALL and len(named) == 1 and named[0].type == "identifier":
            inner = call.child_by_field_name("object")
            if inner is None or inner.type != "method_invocation":
                continue
            inner_method, inner_path, inner_arg, inner_obj = _parts(inner, source, consts)
            if inner_method == "route" and inner_arg is not None:
                mounts.append((node_text(named[0], source), inner_obj, inner_path))
                mount_routes.add((inner.start_byte, inner.end_byte))
        elif method == MOUNT_SUB_ROUTER and len(named) == 2 and named[1].type == "identifier":
            mounts.append((node_text(named[1], source), obj, prefix))
    return mounts, mount_routes


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
                impl = _local_value(impl, node_text(impl, source), source)
            if impl is not None and impl.type == "object_creation_expression":
                typ = impl.child_by_field_name("type")
                return node_text(typ, source) if typ is not None else None
            return None
        node = parent
    return None


def _local_value(at: Node, name: str, source: bytes) -> Node | None:
    """The value given to ``name`` in the method enclosing ``at`` — a local initializer or an
    assignment (``impl = new Impl()``, also to a field) — when exactly one is found."""
    method: Node | None = at
    while method is not None and method.type not in ("method_declaration",
                                                     "constructor_declaration"):
        method = method.parent
    found: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "variable_declarator":
            nm, value = n.child_by_field_name("name"), n.child_by_field_name("value")
            if nm is not None and value is not None and node_text(nm, source) == name:
                found.append(value)
        elif n.type == "assignment_expression":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None and \
                    node_text(left, source).removeprefix("this.") == name:
                found.append(right)
        for c in n.named_children:
            walk(c)

    if method is not None:
        walk(method)
    return found[0] if len(found) == 1 else None


def _qualified(type_name: str, root: Node, source: bytes) -> str:
    """A type as written in this file → its fully-qualified name: a single-type import that
    names it, else this file's package (a dotted name is taken as written)."""
    if "." in type_name:
        return type_name
    package = ""
    for node in root.named_children:
        if node.type == "import_declaration":
            text = node_text(node, source).removeprefix("import").strip().rstrip(";").strip()
            if text.endswith("." + type_name) and not text.startswith("static "):
                return text
        elif node.type == "package_declaration":
            nm = next((c for c in node.named_children
                       if c.type in ("scoped_identifier", "identifier")), None)
            package = node_text(nm, source) if nm is not None else ""
    return f"{package}.{type_name}" if package else type_name


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
    calls = _invocations(root)
    mounts, mount_routes = (
        _sub_router_mounts(calls, source, consts) if b"ubRouter" in source else ([], set())
    )
    bases = sub_router_bases(mounts)
    service_vars = service_builder_vars(source) if b"setAddress" in source else {}

    for call in calls:
        if (call.start_byte, call.end_byte) in suppress:  # wrapper plumbing → honest-null
            continue
        if (call.start_byte, call.end_byte) in mount_routes:  # a sub-router's mount path: wiring
            continue
        info = _classify(call, source, consts, aliases, service_vars)
        if info is None:
            continue
        semantic, method, endpoint, route_kind = info
        unknown_base = False
        receiver = call.child_by_field_name("object")
        recv = node_text(receiver, source) if receiver is not None else ""
        if semantic == "route" and recv in bases:  # a sub-router's route: under its mount
            base = bases[recv]
            if base is None or endpoint is None:
                unknown_base, endpoint = True, None
            else:
                endpoint = join_path(base, endpoint)
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
            names = service_methods(index, _qualified(iface, root, source)) if iface else ()
            handlers = list(names) or [handler]
            # The code that runs is the implementing class's method (usually another file).
            impl = _registered_impl(call, source) if names else None

        def site(name: str | None) -> tuple[int | None, str | None]:
            found_at = locate_method(index, "java", impl, name) if impl else None
            if found_at is None:
                return None, None
            return found_at[1], (found_at[0] if found_at[0] != path else None)

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
