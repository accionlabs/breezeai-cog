"""Fastify route detection. Routes are registered by calling a method on a Fastify instance
(``plugins.py`` decides what is one — never the variable's name alone)::

  fastify.get('/users/:id', { preHandler: auth }, getUser)        → route GET, guards [auth]
  fastify.post('/users', { handler: createUser, schema })         → route POST
  fastify.route({ method: ['GET', 'HEAD'], url: '/x', handler })  → one route per verb
  fastify.get('/ws', { websocket: true }, onSocket)               → route WEBSOCKET (ws)

The endpoint is the route's path under the prefix its instance is served at — the app's (none),
an inline ``register(plugin, { prefix })``, or the ``fastify.plugin-prefixes`` fact for a plugin
another file registers or ``@fastify/autoload`` loads. A prefix that can't be resolved leaves the
endpoint empty (contract §8). The detector returns emits; the registry applies them
(``parsers/emits.py``)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..contract import HTTP_METHODS
from ..emits import Emit
from ..treesitter import node_text
from ..typescript.imports import resolve_const_value
from ..wiring import compose_guards
from .plugins import (
    GUARD_HOOKS, LOOSE, ROOT, UNKNOWN, Scope, _args, _member_call, _unwrap, analyzer,
    guard_names, join_prefix, pair_value, plugin_prefixes,
)

#: Shorthand route methods; ``all`` is every verb (``ANY``).
_VERBS = frozenset({"get", "post", "put", "delete", "patch", "options", "head", "all"})
_FUNCTIONS = ("arrow_function", "function_expression", "function")


def _join_path(prefix: str | None, path: str | None) -> str | None:
    """The served address: the instance's prefix, then the route's path (``/api`` + ``/`` →
    ``/api``). Either unknown → None."""
    if prefix is None or path is None:
        return None
    return join_prefix(prefix, path) or "/"


def _path(node: Node | None, source: bytes, consts: dict[str, str | None]) -> str | None:
    node = _unwrap(node)
    if node is None:
        return None
    return resolve_const_value(node, source, consts)


def _handler(node: Node | None, source: bytes, lines: dict[str, int | None]
             ) -> tuple[str | None, int | None]:
    """The route's handler → (name, handlerLine): a named function (its declaration's line when
    this file declares it once), an inline function (no name; where it starts), or a wrapper
    call's last argument, unwrapped the same way."""
    node = _unwrap(node)
    while node is not None and node.type == "call_expression":
        args = _args(node)
        node = _unwrap(args[-1]) if args else None
    if node is None:
        return None, None
    if node.type in _FUNCTIONS:
        return None, node.start_point[0] + 1
    if node.type in ("identifier", "member_expression"):
        name = node_text(node, source)
        return name, lines.get(name)
    return None, None


def _route_hooks(opts: Node | None, source: bytes) -> list[str]:
    if opts is None or opts.type != "object":
        return []
    return [n for hook in GUARD_HOOKS for n in guard_names(_unwrap(pair_value(opts, hook, source)),
                                                           source)]


def _is_websocket(opts: Node | None, source: bytes) -> bool:
    if opts is None or opts.type != "object":
        return False
    value = pair_value(opts, "websocket", source)
    return value is not None and node_text(value, source) == "true"


def _methods(node: Node | None, source: bytes) -> list[str] | None:
    """``route({ method })`` → its verbs (``'GET'`` / ``['GET', 'POST']``); None when it can't be
    read. A verb outside the contract's HTTP set is left out."""
    node = _unwrap(node)
    if node is None:
        return None
    items = node.named_children if node.type == "array" else [node]
    out: list[str] = []
    for item in items:
        value = resolve_const_value(item, source, {}) if item.type == "string" else None
        if value is None:
            return None
        if value.upper() in HTTP_METHODS and value.upper() not in out:
            out.append(value.upper())
    return out


def _routes(call: Node, method: str, source: bytes, consts: dict[str, str | None],
            lines: dict[str, int | None]
            ) -> list[tuple[str, str | None, str | None, int | None, list[str], str]]:
    """One route call → [(method, path, handler, handlerLine, route-level guards, routeKind)]."""
    args = [_unwrap(a) for a in _args(call)]
    if method == "route":
        opts = args[0] if args else None
        if opts is None or opts.type != "object":
            return []
        verbs = _methods(pair_value(opts, "method", source), source)
        url = pair_value(opts, "url", source) or pair_value(opts, "path", source)
        if not verbs or url is None:
            return []
        path = _path(url, source, consts)
        handler_node = pair_value(opts, "handler", source)
    else:
        if len(args) < 2 or args[0] is None or args[0].type == "object":
            return []
        path = _path(args[0], source, consts)
        opts = args[1] if args[1] is not None and args[1].type == "object" else None
        handler_node = args[2] if len(args) > 2 else (
            pair_value(opts, "handler", source) if opts is not None else args[1])
        verbs = ["ANY" if method == "all" else method.upper()]
    if handler_node is None:
        return []
    handler, line = _handler(handler_node, source, lines)
    hooks = _route_hooks(opts, source)
    if _is_websocket(opts, source):
        return [("WEBSOCKET", path, handler, line, hooks, "ws")]
    return [(verb, path, handler, line, hooks, "route") for verb in verbs]


def detect_fastify(root: Node, source: bytes, path: str, record: FileRecord,
                   index: object | None = None) -> list[Emit]:
    """Fastify route statements of this file (``record`` is read for its functions only)."""
    facts = plugin_prefixes(index)
    if b"astify" not in source and path not in facts.files:
        return []
    index_consts = getattr(index, "const_values", None)
    consts: dict[str, str | None] = index_consts if isinstance(index_consts, dict) else {}
    an = analyzer(root, source, consts)
    loose = None if facts.loose_unknown else ""

    def served(scope: Scope) -> tuple[bool, str | None, tuple[str, ...]]:
        """(is an instance, its prefix, guards from other files)."""
        anchor = scope.anchor
        if anchor == ROOT:
            base: str | None = ""
            outer: tuple[str, ...] = ()
        elif anchor == LOOSE:
            base, outer = loose, ()
        elif anchor == UNKNOWN:
            base, outer = None, ()
        else:
            key = (path, anchor[1])
            if anchor[0] == "plugin?" and key not in facts.bases:
                return False, None, ()
            base = facts.bases.get(key, loose)
            outer = facts.guards.get(key, ())
        if base is None or scope.local is None:
            return True, None, outer
        return True, join_prefix(base, scope.local), outer

    # This file's functions → declaration line; a name declared twice → None.
    lines: dict[str, int | None] = {}
    for f in record.functions:
        lines[f.name] = f.startLine if f.name not in lines else None

    found: list[Emit] = []
    stack = [root]
    calls: list[Node] = []
    while stack:
        n = stack.pop()
        if n.type == "call_expression":
            calls.append(n)
        stack.extend(reversed(n.named_children))
    for call in calls:
        mc = _member_call(call, source)
        if mc is None or (mc[1] not in _VERBS and mc[1] != "route"):
            continue
        scope = an.lookup(call, mc[0])
        if scope is None:
            continue
        is_instance, prefix, outer = served(scope)
        if not is_instance:
            continue
        before = [name for off, name in scope.hooks if off < call.start_byte]
        for method, route_path, handler, line, hooks, kind in _routes(
                call, mc[1], source, consts, lines):
            found.append(Emit(
                call,
                {"semanticType": "route", "framework": "fastify", "routeKind": kind,
                 "method": method, "endpoint": _join_path(prefix, route_path)},
                fill={"handler": handler, "handlerLine": line,
                      "guards": compose_guards(list(outer), list(scope.guards), before, hooks),
                      # Only a route's own hooks assert auth (as for Express); hooks shared by
                      # many routes are often logging or parsing.
                      "authRequired": True if hooks else None},
                enrich="any",
            ))
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = detect_fastify(dc.root, dc.source, dc.path, dc.record, dc.ctx.resolution_index)
    return Detection(found, label="fastify") if found else None


register_detector(Detector(
    name="fastify", gate=Gate("typescript", order=15, skip_fixtures=True), detect=_detect,
    frameworks=("fastify",),
))
