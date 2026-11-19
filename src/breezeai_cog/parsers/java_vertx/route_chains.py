"""Vert.x-web handler chains: one endpoint registered over several statements.

Vert.x-web runs every route matching a request in registration order; each handler calls
``ctx.next()`` to go on. A common style registers one handler per statement on the same method
and path, so one endpoint is spread over several calls::

    router.post().handler(BodyHandler.create());                   // every POST
    router.post(prefix + "/return").handler(SessionHandler.create(o));
    router.post(prefix + "/return").handler(ReturnHandler.create(o));
    router.post(prefix + "/return").handler(SuccessPageHandler.create(o));

The contract records one binding per endpoint ("one binding, one record"): the registrations of
one router, method and path directly in one statement list (the same braces — never across the
branches of an ``if`` or the cases of a ``switch``) are **one** ``route`` record, anchored on
the first registration and spanning the ones that directly follow it (a trailing
``failureHandler`` registration included; never across another endpoint's registration). Its ``handler`` is the last handler; every handler that
runs before it — the path's earlier ones and the path-less ``router.route()`` /
``router.<verb>()`` registrations made before it on the same router — goes into ``guards``,
outermost first. A handler written inline (a lambda, an anonymous class) has no name and is left
out of ``guards``, as Express does.

Only the Vert.x 3+ ``Router`` form chains (``router.get(p).handler(h)``). A Vert.x 2
``RouteMatcher`` call takes its handler as an argument (``rm.get(p, h)``) and the first matching
route wins, so those calls are never grouped.
"""

from __future__ import annotations

from tree_sitter import Node

from ..emits import Emit
from ..treesitter import end_line, node_text
from ..vertx_common import HANDLER_CALLS, HTTP_VERBS, is_route_receiver, router_name

#: Statement lists: registrations directly in the same one run one after the other.
_BLOCKS = frozenset({"block", "constructor_body", "switch_block_statement_group", "program"})
#: A body written without braces (``if (x) router.get(…)…; else router.get(…)…;``) or an
#: expression lambda: the registration is alone in its scope.
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
#: A scope: the byte span of the statement list a registration is directly in.
Scope = tuple[int, int]
#: (scope, router, method, ("=", endpoint) or ("~", path as written)).
GroupKey = tuple[Scope, str, str | None, tuple[str, str]]
#: A path-less registration: (scope, router, method or "route", position, handler nodes).
Middleware = tuple[Scope, str, str, int, list[Node]]


def _scope(node: Node) -> Scope:
    """The statement list ``node`` is directly in (its braces), so registrations in different
    branches — ``if`` / ``else``, ``switch`` cases, a loop body — are never grouped: only
    statements of one list are known to run one after the other."""
    child, parent = node, node.parent
    while parent is not None:
        if parent.type in _BLOCKS:
            return parent.start_byte, parent.end_byte
        if parent.type in _SINGLE:
            return child.start_byte, child.end_byte
        child, parent = parent, parent.parent
    return 0, 0


def _outermost(call: Node) -> Node:
    """The whole chain a call heads: ``router.get(p).handler(a).handler(b)``."""
    node = call
    while (
        (parent := node.parent) is not None
        and parent.type == "method_invocation"
        and parent.child_by_field_name("object") == node
    ):
        node = parent
    return node


def chained_handlers(call: Node, source: bytes) -> list[Node]:
    """The arguments of the ``.handler(…)`` / ``.blockingHandler(…)`` calls chained on ``call``,
    in order (``failureHandler`` is not one: it runs only when a handler fails)."""
    out: list[Node] = []
    node = call
    while (
        (parent := node.parent) is not None
        and parent.type == "method_invocation"
        and parent.child_by_field_name("object") == node
    ):
        name = parent.child_by_field_name("name")
        args = parent.child_by_field_name("arguments")
        if (
            name is not None
            and node_text(name, source) in HANDLER_CALLS
            and args is not None
            and args.named_children
        ):
            out.append(args.named_children[0])
        node = parent
    return out


def _arg_count(call: Node) -> int:
    args = call.child_by_field_name("arguments")
    return len(args.named_children) if args is not None else 0


def _receiver(call: Node, source: bytes) -> str:
    obj = call.child_by_field_name("object")
    return router_name(node_text(obj, source)) if obj is not None else ""


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
        parts = [c for c in node.named_children]
        return node_text(parts[-1], source) if len(parts) >= 2 else None
    if kind in ("identifier", "field_access"):
        return node_text(node, source).rsplit(".", 1)[-1]
    return None


def pathless_middleware(
    calls: list[Node], source: bytes, routers: frozenset[str]
) -> list[Middleware]:
    """``router.route().handler(h)`` / ``router.post().handler(h)``: path-less registrations that
    run before every later route they match; a path-less call with no ``.handler`` (only a
    failure handler) is left out."""
    out: list[Middleware] = []
    for call in calls:
        name = call.child_by_field_name("name")
        if name is None or _arg_count(call) != 0:
            continue
        verb = node_text(name, source)
        if verb != _ANY and verb not in HTTP_VERBS:
            continue
        recv = _receiver(call, source)
        if not recv or not (is_route_receiver(recv.lower()) or recv in routers):
            continue
        handlers = chained_handlers(call, source)
        if handlers:
            out.append((_scope(call), recv, verb, call.start_byte, handlers))
    return out


def groupable(call: Node, source: bytes) -> bool:
    """A Vert.x 3+ route registration whose handlers are chained (``router.get(p).handler(h)``)
    — never a Vert.x 2 ``rm.get(p, h)``."""
    return _arg_count(call) == 1 and bool(chained_handlers(call, source))


def group_key(call: Node, source: bytes, method: str | None, endpoint: str | None) -> GroupKey:
    """Same scope, router and method, and the same path: the resolved address when known, else
    the path expression as written (``prefix + "/return"``)."""
    args = call.child_by_field_name("arguments")
    raw = node_text(args.named_children[0], source) if args is not None else ""
    return (
        _scope(call),
        _receiver(call, source),
        method,
        ("=", endpoint) if endpoint else ("~", raw),
    )


def _span_end(own: list[Node], foreign: list[int]) -> int:
    """Last line of the endpoint's registrations that follow one another from the first, with
    no other route registered in between (a record never spans another endpoint's)."""
    end = _outermost(own[0])
    for call in own[1:]:
        if any(end.end_byte <= pos < call.start_byte for pos in foreign):
            break
        end = _outermost(call)
    return end_line(end)


def merge_chains(
    found: list[Emit],
    routes: list[tuple[GroupKey, int, Node]],
    middleware: list[Middleware],
    failures: list[tuple[GroupKey, Node]],
    source: bytes,
) -> list[Emit]:
    """``found`` with each group of chained route registrations merged into one record.

    ``routes`` are ``(group key, index in found, call)`` in source order; ``failures`` the
    failure-handler-only registrations (no record of their own). The group's record is the last
    registration's emit (its handler is the binding's) moved onto the first one's call, with
    ``guards`` = every named handler that runs before it, and spanning the registrations —
    failure handlers included — that directly follow the first."""
    groups: dict[GroupKey, list[tuple[int, Node]]] = {}
    for key, idx, call in routes:
        groups.setdefault(key, []).append((idx, call))
    positions = [(key, call.start_byte) for key, _, call in routes]
    positions += [(key, call.start_byte) for key, call in failures]
    drop: set[int] = set()
    out = list(found)
    for key, members in groups.items():
        recv, method = key[1], key[2]
        first_idx, first_call = members[0]
        last_idx, last_call = members[-1]
        last_emit = found[last_idx]
        handlers = [h for _, call in members for h in chained_handlers(call, source)]
        # Path-less registrations made before the binding on the same router, matching its
        # method, in a statement list the binding is in (or nested in).
        before = [
            (pos, h)
            for m_scope, m_recv, m_verb, pos, hs in middleware
            if m_scope[0] <= last_call.start_byte < m_scope[1]
            and m_recv == recv
            and pos < last_call.start_byte
            and (m_verb == _ANY or (method is not None and m_verb.upper() == method))
            for h in hs
        ]
        chain = sorted(
            [*before, *((h.start_byte, h) for h in handlers[:-1])], key=lambda item: item[0]
        )
        guards = [n for _, h in chain if (n := handler_name(h, source)) is not None]
        own = sorted(
            [
                *(call for _, call in members),
                *(
                    call
                    for k, call in failures
                    if k == key and call.start_byte > first_call.start_byte
                ),
            ],
            key=lambda call: call.start_byte,
        )
        foreign = [pos for k, pos in positions if k != key] + [m[3] for m in middleware]
        last_line = _span_end(own, foreign)
        if len(members) == 1 and not guards and last_line == end_line(_outermost(first_call)):
            continue  # one registration, nothing before or after it: the record as it is
        fill = dict(last_emit.fill)
        if guards:
            fill["guards"] = list(dict.fromkeys(guards))
        out[first_idx] = Emit(
            first_call,
            dict(last_emit.fields),
            fill=fill,
            enrich=last_emit.enrich,
            extend_to=last_line,
        )
        drop.update(idx for idx, _ in members[1:])
    return [e for i, e in enumerate(out) if i not in drop]
