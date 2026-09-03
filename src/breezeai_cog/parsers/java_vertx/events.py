"""Vert.x event/route detection over the **Java** AST. Vert.x is call-based, so this
walks method invocations and maps them to event/messaging/route semantics:

  eventBus.send/publish/consumer → eventbus_send / eventbus_publish / eventbus_consumer
  vertx.setTimer/setPeriodic     → timer
  vertx.deployVerticle(...)      → verticle_deploy
  ServiceBinder…setAddress(...)  → service_proxy    (+ @ProxyGen interfaces)
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

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, SemanticType, Statement
from ..treesitter import first_line, node_text
from ..vertx_common import (
    EVENTBUS,
    classify_call,
    enclosing_statement,
    is_bus_receiver,
    owner_function,
    render_address,
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
) -> tuple[SemanticType, str | None, str | None, str | None] | None:
    """→ (semanticType, method, endpoint, routeKind) or None. Falls back to an alias-wrapper
    match (``aliases``) when the direct decision table does not classify the call — the
    address is the wrapper call's own first argument, folded like any other."""
    method, first_str, first_arg, obj = _parts(call, source, consts)
    info = classify_call(method, first_str, first_arg, obj)
    if info is not None:
        return info
    semantic = aliases.get(method)
    if semantic is not None:
        return semantic, None, first_str or first_arg, None
    return None


def detect_vertx(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str] | None = None
) -> bool:
    """Enrich/add Vert.x statements on ``record``. Returns True if anything matched.
    ``consts`` (``name → value``) folds symbolic addresses to their String value."""
    matched = False
    fid = file_id(path)
    seen = {s.id for s in record.statements}
    consts = consts or {}

    aliases: dict[str, SemanticType] = {}
    suppress: set[tuple[int, int]] = set()
    if _may_have_wrapper(source):
        aliases, suppress = _wrapper_aliases(root, source)

    for call in _invocations(root):
        if (call.start_byte, call.end_byte) in suppress:  # wrapper plumbing → honest-null
            continue
        info = _classify(call, source, consts, aliases)
        if info is None:
            continue
        semantic, method, endpoint, route_kind = info
        line = call.start_point[0] + 1

        stmt = enclosing_statement(line, record.statements)
        if stmt is not None:  # detection on the same span → enrich in place
            stmt.semanticType = semantic
            stmt.framework = "vertx"
            if method:
                stmt.method = method
            if endpoint:
                stmt.endpoint = endpoint
            if route_kind:
                stmt.routeKind = route_kind
        else:  # inside a lambda (base skips nested scopes) → add a statement
            new_id = disambiguate(statement_id(path, line, call.start_point[1]), seen)
            record.statements.append(Statement(
                id=new_id,
                parentId=owner_function(line, record.functions, fid),
                nodeType=call.type,
                semanticType=semantic,
                text=first_line(node_text(call, source)),
                method=method,
                endpoint=endpoint,
                framework="vertx",
                routeKind=route_kind,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            ))
        matched = True

    for cls in record.classes:  # @ProxyGen service interfaces
        if any(d.name == "ProxyGen" for d in cls.decorators):
            new_id = disambiguate(statement_id(path, cls.startLine, 0), seen)
            record.statements.append(Statement(
                id=new_id, parentId=cls.id, nodeType="synthetic",
                semanticType="service_proxy", text="@ProxyGen", framework="vertx",
                startLine=cls.startLine, endLine=cls.startLine, path=path,
            ))
            matched = True

    return matched
