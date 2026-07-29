"""Shared Vert.x semantic classification for the ``java_vertx`` and ``groovy_vertx``
framework parsers.

Vert.x wiring is *call-based* (no annotations), so detection is: walk method invocations,
map (receiver, method, first-arg) → a semanticType. The **AST extraction** (how a call's
method/receiver/args are read off the node) is grammar-specific and lives in each parser's
``events.py``; the **decision table** below — which call maps to which semantic — is
language-agnostic and shared here so Java and Groovy Vert.x stay in lock-step.

Both Vert.x generations are covered: 3.x (``io.vertx``, ``Router.router(vertx)``,
``eventBus.consumer``) and 2.x (``org.vertx.java`` / ``org.vertx.groovy``, ``RouteMatcher``,
``eventBus().registerHandler``) — the latter is what the P3 webapp-engine API modules use.
"""

from __future__ import annotations

from ..schemas import Function, SemanticType, Statement

#: EventBus method → semanticType. Includes the Vert.x 2.x ``registerHandler`` family
#: (renamed ``consumer`` in 3.x) and ``request`` (3.x send-with-reply → same as ``send``).
EVENTBUS: dict[str, SemanticType] = {
    "send": "eventbus_send",
    "request": "eventbus_send",
    "publish": "eventbus_publish",
    "consumer": "eventbus_consumer",
    "localConsumer": "eventbus_consumer",
    "registerHandler": "eventbus_consumer",
    "registerLocalHandler": "eventbus_consumer",
}
TIMERS = {"setTimer", "setPeriodic"}
HTTP_VERBS = {"get", "post", "put", "delete", "patch", "options", "head"}

#: Bare receiver names denoting a route registrar: a Vert.x 2.x ``RouteMatcher`` (commonly
#: ``route`` / ``rm``) or a 3.x ``Router`` variable. ``"router"`` / ``"routematcher"`` are
#: also matched as substrings, so ``httpRouter`` / ``apiRouteMatcher`` still count.
ROUTE_RECEIVERS = {"route", "rm", "routematcher"}


def is_route_receiver(obj_l: str) -> bool:
    """Whether a (lower-cased) receiver denotes a Router / RouteMatcher."""
    return bool(obj_l) and (
        "router" in obj_l or "routematcher" in obj_l or obj_l in ROUTE_RECEIVERS
    )


def classify_call(
    method: str,
    path: str | None,
    first_arg: str | None,
    obj: str,
    *,
    route_scope: bool = False,
) -> tuple[SemanticType, str | None, str | None, str | None] | None:
    """Map one call to ``(semanticType, method, endpoint, routeKind)`` or ``None``.

    ``path``      the first string-literal argument, already rendered by the caller (Java:
                  literal text; Groovy: GString → ``{name}`` placeholders).
    ``first_arg`` raw text of the first argument — the fallback address for event-bus calls
                  that pass a constant/variable rather than a string literal.
    ``obj``       the receiver text (``""`` for a bare, unqualified call).
    ``route_scope`` marks a *bare* HTTP-verb call (no receiver) that the caller has
                  determined is lexically inside a RouteMatcher scope — the Vert.x 2.x
                  ``rm.with { get('/x', h) }`` idiom — so it should still count as a route.

    Route detection intentionally does **not** require the path to start with ``/``: 2.x
    ``RouteMatcher`` paths are frequently GStrings (``"${prefix}/x"``) whose rendered form
    starts with a placeholder. The receiver check (or ``route_scope``) is the real signal.
    """
    obj_l = obj.lower()

    if method in EVENTBUS and ("bus" in obj_l or obj_l == "eb"):
        return EVENTBUS[method], None, path or first_arg, None
    if method in TIMERS:
        return "timer", None, None, None
    if method == "deployVerticle":
        return "verticle_deploy", None, path or first_arg, None
    if method == "setAddress" and path is not None:
        return "service_proxy", None, path, None
    if method in HTTP_VERBS and path and (is_route_receiver(obj_l) or route_scope):
        return "route", method.upper(), path, "route"
    if method == "route" and path and is_route_receiver(obj_l):
        return "route", None, path, "route"
    return None


def enclosing_statement(line: int, statements: list[Statement]) -> Statement | None:
    """The smallest already-captured statement spanning ``line`` (to enrich in place)."""
    best: Statement | None = None
    best_span: int | None = None
    for s in statements:
        if s.startLine <= line <= s.endLine:
            span = s.endLine - s.startLine
            if best_span is None or span < best_span:
                best, best_span = s, span
    return best


def owner_function(line: int, functions: list[Function], fallback: str) -> str:
    """Id of the smallest function spanning ``line`` (parent for a synthesized statement)."""
    best = None
    best_span: int | None = None
    for f in functions:
        if f.startLine <= line <= f.endLine:
            span = f.endLine - f.startLine
            if best_span is None or span < best_span:
                best, best_span = f, span
    return best.id if best is not None else fallback
