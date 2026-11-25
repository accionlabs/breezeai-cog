"""Vert.x-web middleware: the ``guards`` of a route registration.

Vert.x-web runs every route matching a request in registration order; each handler calls
``ctx.next()`` to go on. Some of those handlers are middleware, run for many routes::

    router.route().handler(LoggerHandler.create());          // every request
    router.post().handler(BodyHandler.create());             // every POST
    router.post(p).handler(CorsHandler.create()).handler(this::save);

Only what is certainly middleware goes into a route's ``guards``, outermost first:

- a **path-less** registration (``router.route()`` for every method, ``router.post()`` and the
  other verbs for that method) made before the route, on the same router, in a statement list
  the route is in or nested in — so in the same method;
- a **Vert.x library handler** (imported from ``io.vertx.ext.web.handler``) chained ahead of the
  route's own handler on the same registration (``.handler(BodyHandler.create()).handler(h)``).

A ``.order(n)`` or ``.last()`` on either side makes the run order differ from the registration
order: that path-less handler is then left out. A handler written inline (a lambda, an anonymous
class) has no name and is left out, as Express does.

The app's own handlers registered one after another on one path are steps of one endpoint,
not guards: ``route_chains.py`` makes such registrations one record with its ``handlerChain``.
"""

from __future__ import annotations

from typing import Callable

from tree_sitter import Node

from ..treesitter import node_text
from ..vertx_common import (FAILURE_HANDLER_CALL, HANDLER_CALLS, HTTP_VERBS, is_route_receiver,
                            router_name)

#: Statement lists: registrations directly in the same one run one after the other.
_BLOCKS = frozenset({"block", "constructor_body", "switch_block_statement_group", "program"})
#: A body written without braces, or an expression lambda: the registration is alone in its scope.
_SINGLE = frozenset(
    {
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "labeled_statement",
        "switch_rule",
        "lambda_expression",
    }
)
#: ``router.route()`` serves every method.
_ANY = "route"
#: Calls that change when a route runs relative to the others.
_REORDER = frozenset({"order", "last"})
#: The package of Vert.x-web's own handlers (``BodyHandler``, ``LoggerHandler``, ``CorsHandler`` …).
_LIBRARY_PACKAGE = "io.vertx.ext.web.handler"
#: A scope: the byte span of the statement list a registration is directly in.
Scope = tuple[int, int]
#: A path-less registration: (scope, router, method or "route", position, handler names).
Middleware = tuple[Scope, str, str, int, list[str]]


def scope_of(node: Node) -> Scope:
    """The statement list ``node`` is directly in (its braces)."""
    child, parent = node, node.parent
    while parent is not None:
        if parent.type in _BLOCKS:
            return parent.start_byte, parent.end_byte
        if parent.type in _SINGLE:
            return child.start_byte, child.end_byte
        child, parent = parent, parent.parent
    return 0, 0


def _chain(call: Node, source: bytes) -> list[tuple[str, Node | None]]:
    """The calls chained on ``call`` (``router.get(p).order(1).handler(h)``): (name, first
    argument), in order."""
    out: list[tuple[str, Node | None]] = []
    node = call
    while (
        (parent := node.parent) is not None
        and parent.type == "method_invocation"
        and parent.child_by_field_name("object") == node
    ):
        name = parent.child_by_field_name("name")
        args = parent.child_by_field_name("arguments")
        first = args.named_children[0] if args is not None and args.named_children else None
        if name is not None:
            out.append((node_text(name, source), first))
        node = parent
    return out


def chained_handlers(call: Node, source: bytes) -> list[Node]:
    """The arguments of the ``.handler(…)`` / ``.blockingHandler(…)`` calls chained on ``call``
    (``failureHandler`` is not one: it runs only when a handler fails)."""
    return [arg for name, arg in _chain(call, source) if name in HANDLER_CALLS and arg is not None]


def failure_args(call: Node, source: bytes) -> list[Node]:
    """The arguments of the ``.failureHandler(…)`` calls chained on ``call``, in order."""
    return [arg for name, arg in _chain(call, source)
            if name == FAILURE_HANDLER_CALL and arg is not None]


#: Chained calls that make a registration one of several alternatives for a path.
_ALTERNATIVES = frozenset({"produces", "consumes", "virtualHost"})


def alternatives(call: Node, source: bytes) -> tuple[str, ...]:
    """``.produces(…)`` / ``.consumes(…)`` / ``.virtualHost(…)`` chained on ``call``, as written,
    sorted: registrations that differ in these are alternatives for a path, not steps."""
    return tuple(sorted(
        f"{name}({node_text(arg, source) if arg is not None else ''})"
        for name, arg in _chain(call, source) if name in _ALTERNATIVES
    ))


def chained_names(call: Node, source: bytes) -> list[str]:
    """The names of the calls chained on ``call``, in order."""
    return [name for name, _ in _chain(call, source)]


def reordered(call: Node, source: bytes) -> bool:
    """``.order(n)`` or ``.last()`` on the registration: it does not run in registration order."""
    return any(name in _REORDER for name, _ in _chain(call, source))


def handler_name(node: Node, source: bytes) -> str | None:
    """A handler's name as written: ``BodyHandler.create()`` → ``BodyHandler`` (the factory's
    class), ``new AuthHandler(…)`` → ``AuthHandler``, ``this::check`` → ``check``, a variable or
    field → its name. An inline lambda or anonymous class has none."""
    kind = node.type
    if kind == "method_invocation":
        obj = node.child_by_field_name("object")
        if obj is None:
            name = node.child_by_field_name("name")
            return node_text(name, source) if name is not None else None
        if obj.type in ("identifier", "field_access", "scoped_identifier"):
            last = node_text(obj, source).rsplit(".", 1)[-1]
            return last if last[:1].isupper() else None
        return None
    if kind == "object_creation_expression":
        if any(c.type == "class_body" for c in node.children):
            return None
        typ = node.child_by_field_name("type")
        return node_text(typ, source).split("<", 1)[0].rsplit(".", 1)[-1] if typ else None
    if kind == "method_reference":
        parts = list(node.named_children)
        return node_text(parts[-1], source) if len(parts) >= 2 else None
    if kind in ("identifier", "field_access"):
        return node_text(node, source).rsplit(".", 1)[-1]
    return None


def library_name(arg: Node, source: bytes, is_library: Callable[[str], bool]) -> str | None:
    """The name of a Vert.x-web library handler built by ``arg`` (``BodyHandler.create()``,
    ``new CorsHandler(…)``, also configured: ``BodyHandler.create().setUploadsDirectory(d)``,
    whose setters return the handler), else None. Only a class can be one: a method reference
    or a variable is the app's whatever its name."""
    while arg.type == "method_invocation" and (
            (inner := arg.child_by_field_name("object")) is not None
            and inner.type in ("method_invocation", "object_creation_expression")):
        arg = inner  # `X.create().setY(…)`: the handler the configuration calls are made on
    if arg.type not in ("method_invocation", "object_creation_expression"):
        return None
    name = handler_name(arg, source)
    return name if name is not None and is_library(name) else None


def library_handlers(
    singles: list[str], wildcards: list[str], package: str, declared: set[str], index: object | None
) -> Callable[[str], bool]:
    """Whether a simple name is a Vert.x-web library handler in this file: imported by name from
    ``io.vertx.ext.web.handler`` (or a subpackage), or reached through
    ``io.vertx.ext.web.handler.*`` when no import by name, class of this file or class of this
    package takes the name first (Java's own lookup order)."""
    named = {imp.rsplit(".", 1)[-1]: imp.rsplit(".", 1)[0] for imp in singles}
    on_demand = _LIBRARY_PACKAGE in wildcards
    fqcns = getattr(index, "fqcn", None) or {}

    def is_library(name: str) -> bool:
        if name in named:
            pkg = named[name]
            return pkg == _LIBRARY_PACKAGE or pkg.startswith(_LIBRARY_PACKAGE + ".")
        return on_demand and name not in declared and f"{package}.{name}" not in fqcns

    return is_library


def pathless_middleware(calls: list[Node], source: bytes, routers: frozenset[str]) -> list[Middleware]:
    """``router.route().handler(h)`` / ``router.post().handler(h)``: path-less registrations,
    run before every later route they match. One with ``.order(n)`` / ``.last()``, or with no
    named ``.handler`` (only a failure handler, or an inline lambda), is left out."""
    out: list[Middleware] = []
    for call in calls:
        name = call.child_by_field_name("name")
        args = call.child_by_field_name("arguments")
        if name is None or (args is not None and args.named_children):
            continue
        verb = node_text(name, source)
        if verb != _ANY and verb not in HTTP_VERBS:
            continue
        obj = call.child_by_field_name("object")
        recv = router_name(node_text(obj, source)) if obj is not None else ""
        if not recv or not (is_route_receiver(recv.lower()) or recv in routers):
            continue
        if reordered(call, source):
            continue
        names = [n for h in chained_handlers(call, source)
                 if (n := handler_name(h, source)) is not None]
        if names:
            out.append((scope_of(call), recv, verb, call.start_byte, names))
    return out


def pathless_calls(calls: list[Node], source: bytes,
                   routers: frozenset[str]) -> list[tuple[Node, str, str]]:
    """Every path-less registration on a router — ``router.route()``, ``router.post()`` … with
    whatever is chained on it (a handler, a failure handler, an order) — as (call, router, verb
    or ``route``): what may run for a request between two steps of a chain."""
    out: list[tuple[Node, str, str]] = []
    for call in calls:
        name = call.child_by_field_name("name")
        args = call.child_by_field_name("arguments")
        if name is None or (args is not None and args.named_children):
            continue
        verb = node_text(name, source)
        if verb != _ANY and verb not in HTTP_VERBS:
            continue
        obj = call.child_by_field_name("object")
        recv = router_name(node_text(obj, source)) if obj is not None else ""
        if recv and (is_route_receiver(recv.lower()) or recv in routers) \
                and _chain(call, source):
            out.append((call, recv, verb))
    return out


def route_guards(
    call: Node, recv: str, method: str | None, middleware: list[Middleware],
    is_library: Callable[[str], bool], source: bytes,
) -> list[str] | None:
    """The ``guards`` of the route registered by ``call`` on router ``recv``: the path-less
    middleware registered before it that matches its method, then the library handlers chained
    ahead of its own handler — outermost first, each name once; None when there are none."""
    names: list[str] = []
    if not reordered(call, source):
        scope_pos = call.start_byte
        for (lo, hi), m_recv, m_verb, pos, hs in middleware:
            if lo <= scope_pos < hi and m_recv == recv and pos < scope_pos and (
                m_verb == _ANY or (method is not None and m_verb.upper() == method)
            ):
                names.extend(hs)
    own = chained_handlers(call, source)[:-1]  # the last one is the route's handler
    names.extend(n for h in own if (n := library_name(h, source, is_library)) is not None)
    return list(dict.fromkeys(names)) or None
