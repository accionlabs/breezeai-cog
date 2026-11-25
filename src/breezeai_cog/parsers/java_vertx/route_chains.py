"""Vert.x-web handler chains (Java): one ``route`` record per endpoint registered over several
statements, with its steps in ``handlerChain`` (entry-point contract §5).

Vert.x-web runs every route that matches a request in registration order; a handler calls
``ctx.next()`` to go on and ``ctx.fail(…)`` to jump to the failure handlers. One endpoint is
often written as a chain of registrations on the same path::

    router.get(prefix + "/request").handler(SessionHandler.create(o));      // 1: checks the login
    router.get(prefix + "/request").handler(RequestPageHandler.create(o));  // 2: sends the page
    router.get(prefix + "/request").failureHandler(FailureHandler.create(o));

Such registrations are **one** record, at the first registration, when the code shows for certain
that they are steps of one endpoint in a known order. Every one of these must hold for every
registration of the group (:func:`chain_emits`); if any fails, the whole group is left as it was,
one record per registration (a partial chain would bring back a wrong endpoint count):

1. the same router variable, in the same statement list (so the same method, and no ``if`` or
   loop that only some of them are in, and none made later in a callback);
2. the same verb and the same path — the same endpoint, written as the same expression (a part
   read at runtime surely the same value only when read the same way, from the same variable);
   no regex route, and no route under a mounted sub-router;
3. one verb form: not mixed with an all-verb ``route(p)`` on the path, and not written
   ``route(HttpMethod.GET, p)`` or ``route(p).method(GET)``;
4. no differing ``.produces()`` / ``.consumes()`` / ``.virtualHost()`` (alternatives, not steps);
5. no ``.order(n)`` / ``.last()``, and nothing else registered on the router between the first
   and the last of them that might match the path (a path-less registration of the verb, a
   regex route, a mount, a path that cannot be told apart from it).

A step that cannot be named or located at all (``handlers.next()``) leaves the group as it was.
The steps are each ``.handler(…)`` / ``.blockingHandler(…)`` in registration order, then the
``.failureHandler(…)``\\ s on the path with ``phase: "failure"``. A Vert.x-web library handler
(``BodyHandler``) ahead of the first app step goes into ``guards``; after one it stays a step,
by name. The record's ``handler`` / ``handlerLine`` / ``handlerPath`` are the first step's.
``handlerChain`` is written when there are 2 or more steps or a failure handler.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass

from tree_sitter import Node

from ...schemas import HandlerStep
from ..emits import Emit
from ..handler_sites import handler_info, handler_location, locate_method
from .handler_factories import FactoryCalls
from .java_syntax import anonymous_handle, handler_class
from .route_guards import Scope, handler_name, library_name

#: ``phase`` of a failure handler on the route's own path.
FAILURE = "failure"
#: A registration form a chain is made of: one verb (``get(p)``) or every verb (``route(p)``).
#: Anything else (``route(HttpMethod.GET, p)``, ``.method(…)``, a regex route, a 2.x
#: ``RouteMatcher`` route with its handler as an argument) is ``other``.
Form = str
#: A runtime part of an endpoint: ``${pathPrefix}``.
_RUNTIME = re.compile(r"(\$\{[^}]*\})")


@dataclass(slots=True)
class Registration:
    """One route registration on a router, as the detector saw it."""

    call: Node
    #: Index of its record in the detector's output; -1 for a failure-handler-only registration
    #: (no record of its own).
    slot: int
    router: str
    #: The verb, upper case; None for every verb (``route(p)``).
    method: str | None
    form: Form
    #: The path as compared: the endpoint (``${pathPrefix}/request``), else the path as written.
    path: str | None
    #: The path argument's source text: registrations are steps of one path only when it is the
    #: same — ``path`` and ``config().getString("p")`` may both render ``${config:p}``, but only
    #: the same expression in the same statement list surely reads the same value.
    written: str | None
    scope: Scope
    handlers: list[Node]
    failures: list[Node]
    reordered: bool
    #: ``.produces(…)`` / ``.consumes(…)`` / ``.virtualHost(…)`` as written, sorted.
    alternatives: tuple[str, ...]
    #: False under a mounted sub-router, or with no path to compare.
    chainable: bool
    emit: Emit | None

    @property
    def position(self) -> int:
        return self.call.start_byte


@dataclass(slots=True, frozen=True)
class Other:
    """Another registration on a router: may a request for the chain's path reach it?"""

    position: int
    router: str
    #: ``route`` (with a path), ``pathless`` (``router.post()``), ``regex``, ``mount``.
    kind: str
    method: str | None = None
    path: str | None = None


@dataclass(slots=True)
class Locator:
    """Names and locates one handler argument as the route's own handler is located."""

    source: bytes
    path: str
    methods: dict[str, int | None]
    index: object | None
    built_by: FactoryCalls
    is_library: Callable[[str], bool]

    def library(self, arg: Node) -> str | None:
        """The name of a Vert.x-web library handler built here, else None (:func:`library_name`)."""
        return library_name(arg, self.source, self.is_library)

    def __call__(self, arg: Node, phase: str | None = None) -> HandlerStep:
        own = self.library(arg)
        if own is not None:
            return HandlerStep(name=own, phase=phase)  # Vert.x-web's own: nothing to open
        name = handler_name(arg, self.source)
        handler, line = handler_info(arg, self.source, self.methods)
        where: str | None = None
        if handler is None:
            anon = anonymous_handle(arg, self.source)
            if anon is not None:  # `new Handler<…>() { void handle(…) }`
                handler, line = "handle", anon
        if handler is None and line is None:  # not written inline: a class or a factory
            impl = handler_class(arg, self.source) or self.built_by.built(arg)
            if impl:
                # A Vert.x `Handler` has one method, `handle`: the code that runs.
                handler = "handle"
                line, where = handler_location(
                    locate_method(self.index, "java", impl, "handle"), self.path)
        return HandlerStep(name=name, handler=handler, handlerLine=line, handlerPath=where,
                           phase=phase)


def _might_match(other: Other, method: str | None, path: str) -> bool:
    """Whether a request for ``method`` + ``path`` may reach ``other`` (False only when certain)."""
    if other.method is not None and method is not None and other.method != method:
        return False  # another verb
    if other.kind != "route" or other.path is None:
        return True  # path-less, regex, mount, or a path that is not known
    return not _different_paths(other.path, path)


def _different_paths(a: str, b: str) -> bool:
    """True when two route paths certainly match no common request: after the runtime parts
    they share at the start (``${pathPrefix}``), both are plain text, and the texts differ."""
    ta, tb = _RUNTIME.split(a), _RUNTIME.split(b)
    i = 0
    while i < min(len(ta), len(tb)) and ta[i] == tb[i] and _RUNTIME.fullmatch(ta[i] or "x"):
        i += 1
    while i < min(len(ta), len(tb)) and ta[i] == "" and tb[i] == "":
        i += 1
    rest_a, rest_b = "".join(ta[i:]), "".join(tb[i:])
    if any(c in rest for rest in (rest_a, rest_b) for c in "${}*:"):
        return False  # a runtime part, a wildcard or a path parameter: cannot be told apart
    return rest_a.rstrip("/") != rest_b.rstrip("/")  # `/x` also serves `/x/`


def chain_emits(
    regs: list[Registration], others: list[Other], locate: Locator,
    pathless_guards: Callable[[Registration], list[str]],
) -> dict[int, Emit | None]:
    """Slot → its new record (the first registration of a chain) or None (another registration
    of a chain: no record of its own). Slots of registrations not in a chain are absent."""
    by_path: dict[tuple[str, str], set[Form]] = {}
    for reg in regs:
        if reg.path is not None:
            by_path.setdefault((reg.router, reg.path), set()).add(reg.form)
    groups: dict[tuple[object, ...], list[Registration]] = {}
    for reg in regs:
        if reg.form == "other" or reg.path is None or not reg.router:
            continue
        groups.setdefault((reg.router, reg.method, reg.path, reg.written, reg.form),
                          []).append(reg)
    every = others + [Other(r.position, r.router, "regex" if r.form == "other" and r.path is None
                            else "route", r.method, r.path) for r in regs]

    out: dict[int, Emit | None] = {}
    for (router, method, path, _, _), members in groups.items():
        members.sort(key=lambda r: r.position)
        routes = [r for r in members if r.emit is not None]
        if not routes:
            continue  # only failure handlers on the path: error wiring, as before
        if len(by_path[(router, path)]) > 1:
            continue  # mixed with `route(p)` on the path (check 3)
        if len({r.written for r in regs if (r.router, r.path) == (router, path)}) > 1:
            continue  # the same endpoint written two ways: not surely one path
        if not _certain(members, every, router, method, path):
            continue
        record = _record(members, routes[0], locate, pathless_guards)
        if record is None:
            continue
        out[routes[0].slot] = record
        for reg in routes[1:]:
            out[reg.slot] = None
    return out


def _certain(members: list[Registration], every: list[Other], router: str,
             method: str | None, path: str) -> bool:
    """Checks 1, 2 (sub-router), 4 and 5 of the module docstring, over the whole group."""
    first, last = members[0], members[-1]
    if any(not r.chainable or r.reordered for r in members):
        return False
    if len({r.scope for r in members}) != 1 or len({r.alternatives for r in members}) != 1:
        return False
    own = {r.position for r in members}
    return not any(
        o.router == router and first.position < o.position < last.position
        and o.position not in own and _might_match(o, method, path)
        for o in every
    )


def _record(members: list[Registration], first: Registration, locate: Locator,
            pathless_guards: Callable[[Registration], list[str]]) -> Emit | None:
    """The chain's one record, or None when it has no app step (only library handlers)."""
    library: list[str] = []
    steps: list[HandlerStep] = []
    for reg in members:
        for arg in reg.handlers:
            own = locate.library(arg)
            if not steps and own is not None:
                library.append(own)  # the framework's own, ahead of the first app step
            else:
                steps.append(locate(arg))
    failures = [locate(arg, FAILURE) for reg in members for arg in reg.failures]
    if not steps:
        return None
    if any(s.name is None and s.handler is None and s.handlerLine is None
           for s in (*steps, *failures)):
        return None  # a step known by nothing (`factory().build()`): not a chain a reader can use
    if len(members) == 1 and len(steps) == 1 and not failures:
        return None  # one registration with one app step: its record is already right
    assert first.emit is not None
    emit = dataclasses.replace(first.emit, fields=dict(first.emit.fields),
                               fill=dict(first.emit.fill))
    head = steps[0]
    emit.fill["handler"] = head.handler
    emit.fill["handlerLine"] = head.handlerLine
    emit.fill["handlerPath"] = head.handlerPath
    guards = list(dict.fromkeys([*pathless_guards(first), *library]))
    emit.fill["guards"] = guards or None
    if len(steps) >= 2 or failures:
        emit.fill["handlerChain"] = [*steps, *failures]
    return emit
