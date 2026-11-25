"""Vert.x-web handler chains: one ``route`` record per endpoint, with every handler that runs for it.

Vert.x-web runs every route that matches a request in registration order; a handler calls
``ctx.next()`` to go on, ``ctx.fail(…)`` to jump to the route's failure handlers. A common style
registers one handler per statement on the same method and path, so one endpoint is a chain
spread over several statements::

    router.post().handler(BodyHandler.create());                       // every POST
    router.post(prefix + "/return").handler(SessionHandler.create(o));  // 1
    router.post(prefix + "/return").handler(ReturnHandler.create(o));   // 2: the request ends here
    router.post(prefix + "/return").failureHandler(FailureHandler.create(o));

Registrations on the same router, in the same statement list, with the same method and path
(the resolved ``endpoint``, else the path as written) are one :class:`Registration` group and
one record (:func:`group_emits`):

- ``startLine`` is the first registration's, ``endLine`` the last one's (failure handlers
  included); ``handler`` / ``handlerLine`` / ``handlerPath`` are the last handler's — where the
  request ends;
- ``handlerChain`` lists every handler that runs, in registration order: the route's own
  (``scope: route``, a fluent ``.handler(a).handler(b)`` giving one step each) and the path-less
  registrations that match it (``scope: router``: ``router.route()`` for every method,
  ``router.post()`` for POST) made earlier in the same or an enclosing statement list —
  interleaved by where each was registered;
- ``failureHandlers`` lists the ``.failureHandler(h)`` of the group, which never are routes;
- ``guards`` keeps its meaning: the path-less handlers and the Vert.x library handlers ahead of
  the last one, each name once.

A step names the handler as written (``BodyHandler.create()`` → ``BodyHandler``, ``this::save``
→ ``save``) and locates it like a route's handler: a Vert.x library handler and a handler the
repo does not declare have no ``handlerPath``; an inline lambda has no name, only its line.

A registration with ``.order(n)`` / ``.last()`` runs out of registration order and a 2.x
``RouteMatcher`` route takes one handler only: each keeps a record of its own, as before; a
RouteMatcher route's ``handlerChain`` is its one handler.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from tree_sitter import Node

from ...schemas import FailureHandler, HandlerStep
from ..emits import Emit
from ..handler_sites import handler_info, handler_location, locate_method
from ..treesitter import end_line
from .handler_factories import FactoryCalls
from .java_syntax import anonymous_handle, handler_class
from .route_guards import ANY, Middleware, Scope, handler_name

#: ``router`` (a path-less registration) or ``route`` (the route's own).
StepScope = Literal["router", "route"]
#: ``(position, n, scope, registration line, argument)`` of one handler, sorted into run order.
Pending = tuple[int, int, StepScope, int, Node]
#: ``(name, handlerLine, handlerPath)`` of one handler argument.
Located = tuple[str | None, int | None, str | None]


@dataclass(slots=True)
class Locator:
    """Names and locates one handler argument the way a route's handler is located."""

    source: bytes
    path: str
    methods: dict[str, int | None]
    index: object | None
    built_by: FactoryCalls
    is_library: Callable[[str], bool]

    def __call__(self, arg: Node) -> Located:
        name = handler_name(arg, self.source)
        if name is not None and self.is_library(name):
            return name, None, None  # Vert.x-web's own handler: nothing in the repo to lead to
        handler, line = handler_info(arg, self.source, self.methods)
        if handler is not None:
            return name or handler, line, None
        anon = anonymous_handle(arg, self.source)
        if anon is not None:
            return None, anon, None
        impl = handler_class(arg, self.source) or self.built_by.built(arg)
        if impl:
            # A Vert.x `Handler` has one method, `handle`: the code that runs.
            found, where = handler_location(locate_method(self.index, "java", impl, "handle"),
                                            self.path)
            return name or impl, found, where
        return name, line, None  # an inline lambda: its line; anything else: its name only


@dataclass(slots=True)
class Registration:
    """One route registration: the record it would be alone, and what joins it to others."""

    call: Node
    #: Index of its record in the detector's output, kept so a group's record keeps its place.
    slot: int
    #: (statement list, router, method, path, regex, unknown base); None when it never groups.
    key: tuple[object, ...] | None
    scope: Scope
    recv: str
    method: str | None
    emit: Emit | None
    handlers: list[Node] = field(default_factory=list)
    failures: list[Node] = field(default_factory=list)

    @property
    def position(self) -> int:
        return self.call.start_byte

    @property
    def line(self) -> int:
        return self.call.start_point[0] + 1


def _matches(m: Middleware, reg: Registration) -> bool:
    """Whether path-less middleware ``m`` runs before ``reg``: same router, a statement list
    ``reg`` is in or nested in, registered earlier, for its method."""
    lo, hi = m.scope
    return (m.router == reg.recv and lo <= reg.position < hi and m.position < reg.position
            and (m.verb == ANY or (reg.method is not None and m.verb.upper() == reg.method)))


def handler_steps(handlers: list[Pending],
                  locate: Callable[[Node], Located]) -> list[HandlerStep]:
    """``(position, n, scope, line, argument)`` → the steps, in run order."""
    out: list[HandlerStep] = []
    for order, (_, _, scope, line, arg) in enumerate(sorted(handlers, key=lambda h: h[:2])):
        name, at, where = locate(arg)
        out.append(HandlerStep(order=order, scope=scope, registeredAt=line, name=name,
                               handlerPath=where, handlerLine=at))
    return out


def group_emits(regs: list[Registration], middleware: list[Middleware],
                locate: Locator) -> dict[int, Emit]:
    """Slot → record: one per group of registrations (see the module docstring), at the
    slot of its first registration. Slots of the other members are absent (no record)."""
    groups: dict[tuple[object, ...], list[Registration]] = {}
    for reg in regs:
        groups.setdefault(reg.key if reg.key is not None else ("alone", reg.slot), []).append(reg)
    out: dict[int, Emit] = {}
    for members in groups.values():
        routes = [r for r in members if r.emit is not None]
        if not routes:
            continue  # a failure handler on a path with no handler: error wiring only
        first, last = routes[0], routes[-1]
        handlers: list[Pending] = [(r.position, n, "route", r.line, arg)
                    for r in routes for n, arg in enumerate(r.handlers)]
        if first.key is not None:
            handlers += [(m.position, n, "router", m.line, arg) for m in middleware
                         if _matches(m, last) for n, arg in enumerate(m.handlers)]
        steps = handler_steps(handlers, locate)
        emit = _copy(first.emit)
        assert last.emit is not None
        for name in ("handler", "handlerLine", "handlerPath"):
            emit.fill[name] = last.emit.fill.get(name)
        if steps:
            emit.fill["handlerChain"] = steps
            emit.fill["guards"] = _guards(steps, locate.is_library)
        failures = [FailureHandler(registeredAt=r.line, name=n, handlerPath=w, handlerLine=a)
                    for r in members for n, a, w in map(locate, r.failures)]
        if failures:
            emit.fill["failureHandlers"] = failures
        last_line = max(end_line(r.call) for r in members)
        if last_line > end_line(first.call):
            emit.fields["endLine"] = last_line
        out[first.slot] = emit
    return out


def _guards(steps: list[HandlerStep], is_library: Callable[[str], bool]) -> list[str] | None:
    """What certainly is middleware ahead of the last handler: the path-less steps and the
    Vert.x library handlers, outermost first, each name once."""
    names = [s.name for s in steps[:-1]
             if s.name is not None and (s.scope == "router" or is_library(s.name))]
    return list(dict.fromkeys(names)) or None


def _copy(emit: Emit | None) -> Emit:
    assert emit is not None
    return Emit(emit.node, dict(emit.fields), fill=dict(emit.fill), enrich=emit.enrich)


def middleware_emit(m: Middleware, locate: Locator) -> Emit:
    """The record of a path-less registration: ``routeKind: middleware``, no ``endpoint``;
    its handler the last one it chains, ``handlerChain`` all of them."""
    steps = handler_steps([(m.position, n, "router", m.line, arg)
                           for n, arg in enumerate(m.handlers)], locate)
    last = steps[-1] if steps else None
    fill = {"method": None if m.verb == ANY else m.verb.upper(), "routeKind": "middleware",
            "handler": last.name if last else None,
            "handlerLine": last.handlerLine if last else None,
            "handlerPath": last.handlerPath if last else None,
            "handlerChain": steps or None}
    return Emit(m.call, {"semanticType": "route", "framework": "vertx", "endpoint": None},
                fill=fill, enrich="any")


def error_handler_emit(call: Node, status: int | None, arg: Node | None,
                       locate: Locator) -> Emit:
    """``router.errorHandler(404, h)``: ``routeKind: error_handler`` with its ``statusCode``."""
    name, line, where = locate(arg) if arg is not None else (None, None, None)
    fill = {"routeKind": "error_handler", "statusCode": status, "handler": name,
            "handlerLine": line, "handlerPath": where}
    return Emit(call, {"semanticType": "route", "framework": "vertx", "endpoint": None,
                       "method": None}, fill=fill, enrich="any")
