"""Shared Vert.x semantic classification for the ``java_vertx`` and ``groovy_vertx``
framework parsers.

Vert.x wiring is *call-based* (no annotations), so detection is: walk method invocations,
map (receiver, method, first-arg) → a semanticType. The **AST extraction** (how a call's
method/receiver/args are read off the node) is grammar-specific and lives in each parser's
``events.py``; the **decision table** below — which call maps to which semantic — is
language-agnostic and shared here so Java and Groovy Vert.x stay in lock-step.

Both Vert.x generations are covered: 3.x (``io.vertx``, ``Router.router(vertx)``,
``eventBus.consumer``) and 2.x (``org.vertx.java`` / ``org.vertx.groovy``, ``RouteMatcher``,
``eventBus().registerHandler``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from tree_sitter import Node

from ..schemas import Function, SemanticType
from .constfold import init_tokens, resolve_tokens
from .emits import Emit
from .statements_common import render_concat
from .treesitter import node_text

#: Renders a language's ``string_literal`` node to a path (GString-aware in Groovy).
StringRender = Callable[[Node, bytes], "str | None"]

#: EventBus method → semanticType. Includes the Vert.x 2.x ``registerHandler`` family
#: (renamed ``consumer`` in 3.x), ``sendWithTimeout`` (2.x send-with-reply-and-timeout), and
#: ``request`` (3.x send-with-reply → same as ``send``).
EVENTBUS: dict[str, SemanticType] = {
    "send": "eventbus_send",
    "request": "eventbus_send",
    "sendWithTimeout": "eventbus_send",
    "publish": "eventbus_publish",
    "consumer": "eventbus_consumer",
    "localConsumer": "eventbus_consumer",
    "registerHandler": "eventbus_consumer",
    "registerLocalHandler": "eventbus_consumer",
}
#: Timer calls → ``method``: a one-shot delay or a repeating period. The first argument (the
#: delay in milliseconds) is the ``endpoint`` when it is a number literal; anything else is
#: unknown → empty.
TIMERS = {"setTimer": "TIMER", "setPeriodic": "PERIODIC"}
HTTP_VERBS = {"get", "post", "put", "delete", "patch", "options", "head"}

#: Service proxies: the builder ``setAddress(...)`` is called on decides the side. The service
#: (``ServiceBinder…register``) consumes the address; a client (``ServiceProxyBuilder…build``)
#: sends to it. Any other receiver is not known to be either → no record.
SERVICE_SIDES: dict[str, SemanticType] = {
    "ServiceBinder": "eventbus_consumer",
    "ServiceProxyBuilder": "eventbus_send",
}
_SERVICE_VAR = re.compile(rb"\b(ServiceBinder|ServiceProxyBuilder)\s+(\w+)\s*=")


def service_builder_vars(source: bytes) -> dict[str, str]:
    """Variable name → builder type for ``ServiceBinder b = …`` / ``ServiceProxyBuilder c = …``
    (a name declared with both → dropped: unsure)."""
    out: dict[str, str | None] = {}
    for typ, name in _SERVICE_VAR.findall(source):
        key, value = name.decode(), typ.decode()
        out[key] = value if out.get(key, value) == value else None
    return {k: v for k, v in out.items() if v is not None}


def _service_side(obj: str, service_vars: dict[str, str] | None) -> SemanticType | None:
    for builder, side in SERVICE_SIDES.items():
        if f"new {builder}" in obj:
            return side
    builder_of = (service_vars or {}).get(obj.strip())
    return SERVICE_SIDES.get(builder_of) if builder_of is not None else None

#: Bare receiver names denoting a route registrar: a Vert.x 2.x ``RouteMatcher`` (commonly
#: ``route`` / ``rm``) or a 3.x ``Router`` variable. ``"router"`` / ``"routematcher"`` are
#: also matched as substrings, so ``httpRouter`` / ``apiRouteMatcher`` still count.
ROUTE_RECEIVERS = {"route", "rm", "routematcher"}


def is_route_receiver(obj_l: str) -> bool:
    """Whether a (lower-cased) receiver denotes a Router / RouteMatcher."""
    return bool(obj_l) and (
        "router" in obj_l or "routematcher" in obj_l or obj_l in ROUTE_RECEIVERS
    )


def is_bus_receiver(obj: str) -> bool:
    """Whether a receiver denotes an EventBus (``vertx.eventBus()``, ``eb``, a ``…Bus`` var).
    The single source of truth for ``classify_call`` and the wrapper detector alike, so an
    EventBus call and a wrapper that forwards to one are recognised by the same rule."""
    obj_l = obj.lower()
    return "bus" in obj_l or obj_l == "eb"


def classify_call(
    method: str,
    path: str | None,
    first_arg: str | None,
    obj: str,
    *,
    route_scope: bool = False,
    service_vars: dict[str, str] | None = None,
) -> tuple[SemanticType, str | None, str | None, str | None] | None:
    """Map one call to ``(semanticType, method, endpoint, routeKind)`` or ``None``.

    ``path``      the first string-literal argument, already rendered by the caller (Java:
                  literal text; Groovy: GString → ``{name}`` placeholders).
    ``first_arg`` raw text of the first argument — the fallback name for ``deployVerticle``
                  (event-bus addresses never fall back to it: unresolved → empty).
    ``obj``       the receiver text (``""`` for a bare, unqualified call).
    ``service_vars`` variable → service-proxy builder type (:func:`service_builder_vars`), so a
                  ``setAddress`` on a builder held in a variable still finds its side.
    ``route_scope`` marks a *bare* HTTP-verb call (no receiver) that the caller has
                  determined is lexically inside a RouteMatcher scope — the Vert.x 2.x
                  ``rm.with { get('/x', h) }`` idiom — so it should still count as a route.

    Route detection intentionally does **not** require the path to start with ``/``: 2.x
    ``RouteMatcher`` paths are frequently GStrings (``"${prefix}/x"``) whose rendered form
    starts with a placeholder. The receiver check (or ``route_scope``) is the real signal.
    """
    obj_l = obj.lower()

    if method in EVENTBUS and is_bus_receiver(obj):
        # The address is resolved or empty — never the symbol or a {…} placeholder (Q15): a
        # consumer and its senders on the same unresolved symbol both stay empty.
        return EVENTBUS[method], None, _known_path(path) if path else None, None
    if method in TIMERS:
        delay = (first_arg or "").replace("_", "").rstrip("lL")
        return "timer", TIMERS[method], delay if delay.isdigit() else None, None
    if method == "deployVerticle":
        return "verticle_deploy", None, path or first_arg, None
    if method == "setAddress":
        side = _service_side(obj, service_vars)
        if side is None:
            return None
        return side, None, _known_path(path) if path else None, None
    if method in HTTP_VERBS and path and (is_route_receiver(obj_l) or route_scope):
        return "route", method.upper(), _known_path(path), "route"
    if method == "route" and path and is_route_receiver(obj_l):
        return "route", None, _known_path(path), "route"
    return None


def _known_path(path: str) -> str | None:
    """A route path, or None when a part is unknown. Vert.x path parameters are ``:id``, so a
    ``{…}`` in a rendered path is a placeholder for an unresolved code value (a GString
    interpolation or a runtime concatenation): the address is unknown — never a code name."""
    return None if "{" in path else path


def _addr_leaf(node: Node, source: bytes, consts: dict[str, str], string_render: StringRender) -> str | None:
    """Leaf renderer for :func:`render_address`'s concatenation walk: a string literal renders
    (GString-aware), a constant resolves to its value, anything else → ``None`` so
    ``render_concat`` inserts a ``{name}`` placeholder."""
    if node.type == "string_literal":
        return string_render(node, source)
    if node.type == "binary_expression":
        return render_concat(node, source, lambda n, s: _addr_leaf(n, s, consts, string_render))
    return consts.get(node_text(node, source))


def render_address(
    node: Node, source: bytes, consts: dict[str, str], string_render: StringRender
) -> str | None:
    """Resolve a Vert.x address/path argument to a string. A fully-constant expression folds to
    its exact value; a string literal renders via ``string_render`` (GString-aware); a
    concatenation with runtime parts renders with ``{name}`` placeholders (constants folded,
    literals kept) — matching how GString paths render. Returns ``None`` for a bare runtime
    variable, so the caller keeps its symbol fallback."""
    tokens = init_tokens(node, source)
    if tokens is not None:
        exact = resolve_tokens(tokens, consts)
        if exact is not None:
            return exact
    if node.type == "string_literal":
        return string_render(node, source)
    if node.type == "binary_expression":
        return render_concat(node, source, lambda n, s: _addr_leaf(n, s, consts, string_render))
    return None


# ── Sub-routers ─────────────────────────────────────────────────────────────────────────────
#: ``router.route("/api/*").subRouter(sub)`` (4.x) / ``router.mountSubRouter("/api", sub)``
#: (3.x) mount ``sub`` under a prefix: wiring, not a route. ``sub``'s routes are served under it.
SUB_ROUTER_CALL = "subRouter"
MOUNT_SUB_ROUTER = "mountSubRouter"

#: A sub-router mount: (sub-router variable, parent router receiver, prefix — None when the
#: mount path does not resolve to a literal).
SubRouterMount = tuple[str, str, str | None]


def _mount_prefix(path: str) -> str:
    """``/api/*`` → ``/api``: the wildcard only says "everything under"."""
    p = path[:-1] if path.endswith("*") else path
    return p.rstrip("/") or "/"


def sub_router_bases(mounts: Iterable[SubRouterMount]) -> dict[str, str | None]:
    """Sub-router variable → the base its routes are served under, composed through nested
    sub-routers (``api`` on ``router`` at ``/api``, ``v1`` on ``api`` at ``/v1`` → ``/api/v1``).
    None = a mount path on the way does not resolve (the routes' address is unknown). A
    sub-router mounted at two different prefixes, or in a cycle, is absent: its routes keep
    their own path (honest-null)."""
    local: dict[str, tuple[str, str | None] | None] = {}
    for sub, parent, prefix in mounts:
        # Vert.x path parameters are `:id`; a `{name}` in a rendered mount path is an unresolved
        # part (render_address placeholder) — the prefix is unknown.
        known = prefix is not None and "{" not in prefix
        entry = (parent, _mount_prefix(prefix) if known and prefix is not None else None)
        local[sub] = entry if local.get(sub, entry) == entry else None

    unknown = object()

    def base(var: str, seen: frozenset[str]) -> object:
        entry = local.get(var)
        if entry is None or var in seen:
            return None  # ambiguous, or a cycle
        parent, prefix = entry
        if prefix is None:
            return unknown
        if parent in local:
            above = base(parent, seen | {var})
            if above is None or above is unknown:
                return above
            return join_path(str(above), prefix)
        return join_path("", prefix)

    out: dict[str, str | None] = {}
    for var in local:
        b = base(var, frozenset())
        if b is unknown:
            out[var] = None
        elif isinstance(b, str):
            out[var] = b
    return out


def join_path(base: str, local: str) -> str:
    """``/api`` + ``/items`` → ``/api/items``; single slashes, empties ignored."""
    parts = [p.strip("/") for p in (base, local) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


# ── Handler of a route / consumer ───────────────────────────────────────────────────────────
#: Chained calls that attach the handler: ``router.get("/x").handler(h)``. A chain may attach
#: several (``.handler(BodyHandler.create()).handler(this::create)``); the last one is the
#: handler, the earlier ones run before it.
HANDLER_CALLS = frozenset({"handler", "blockingHandler"})
#: Inline handlers: a Java lambda, a Groovy closure.
_INLINE = frozenset({"lambda_expression", "closure"})
#: Method references: Java ``this::h``, Groovy ``this::h`` / ``this.&h``.
_METHOD_REFS = frozenset({"method_reference", "method_reference_expression",
                          "method_pointer_expression"})
#: Semantic types whose handler is an entry point's handler (outbound reply callbacks are not).
HANDLER_SEMANTICS = frozenset({"route", "eventbus_consumer", "timer"})


def handler_info(
    arg: Node | None, source: bytes, methods: dict[str, int | None]
) -> tuple[str | None, int | None]:
    """(handler, handlerLine) for a handler argument (contract §5).

    - Inline lambda / closure / anonymous ``new Handler<>() {…}``: no name; its line.
    - ``this::h`` / ``this.&h``: ``h``, line of its declaration in this file (``methods``).
    - ``other::h`` / a handler variable: the name; declared elsewhere, so no line.
    - Anything else (a factory call ``Handler.create()``): nothing — honest-null."""
    if arg is None:
        return None, None
    if arg.type in _INLINE or (arg.type == "object_creation_expression"
                               and any(c.type == "class_body" for c in arg.named_children)):
        return None, arg.start_point[0] + 1
    if arg.type in _METHOD_REFS:
        text = node_text(arg, source)
        sep = "::" if "::" in text else ".&"
        receiver, _, name = text.rpartition(sep)
        name = name.strip()
        if not name.isidentifier():
            return None, None
        return name, methods.get(name) if receiver.strip() == "this" else None
    if arg.type == "identifier":
        return node_text(arg, source), None
    return None, None


def method_lines(functions: Iterable[Function]) -> dict[str, int | None]:
    """Method name → declaration line for the file's methods (a name declared twice → None)."""
    out: dict[str, int | None] = {}
    for f in functions:
        out[f.name] = f.startLine if f.name not in out else None
    return out


def vertx_emit(
    call: Node, semantic: str, method: str | None, endpoint: str | None,
    route_kind: str | None, handler: str | None, handler_line: int | None,
    *, unknown_base: bool = False,
) -> Emit:
    """A Vert.x detection for :func:`~.emits.apply`: it enriches whatever statement is on the
    call's line (``any``) — the type and framework always, the rest only where known — else
    it is a new record (a call inside a lambda, which the base parser skips). An unresolvable
    sub-router mount clears the address (unknown)."""
    fill: dict[str, Any] = {"method": method, "routeKind": route_kind, "handler": handler,
                            "handlerLine": handler_line}
    fields: dict[str, Any] = {"semanticType": semantic, "framework": "vertx"}
    if unknown_base:
        fields["endpoint"] = None
    else:
        fill["endpoint"] = endpoint
    return Emit(call, fields, fill=fill, enrich="any")
