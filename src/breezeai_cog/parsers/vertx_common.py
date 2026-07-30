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

from collections.abc import Callable

from tree_sitter import Node

from ..schemas import Function, SemanticType, Statement
from .constfold import init_tokens, resolve_tokens
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
