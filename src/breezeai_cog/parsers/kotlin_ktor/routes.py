"""Ktor HTTP and WebSocket route detection from the routing DSL.

Ktor registers routes via a lambda DSL:
    routing {
        get("/path") { ... }
        route("/prefix") {
            post("/sub") { ... }
        }
    }

detect_ktor_routes() walks the AST of an already-parsed FileRecord's tree to find
these call_expression nodes and emit them as Statement records.
"""

from __future__ import annotations

from collections.abc import Callable

from tree_sitter import Node

from ..emits import Emit
from ..treesitter import node_text

_HTTP_VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
#: ``webSocket("/ws") { … }`` / ``webSocketRaw(…)``: a WebSocket upgrade (method ``WEBSOCKET``,
#: ``routeKind: ws``). A client session is ``client.webSocket(…)`` — a member call, never matched.
_WS_BUILDERS = frozenset({"webSocket", "webSocketRaw"})


#: An argument that is a path expression whose value cannot be resolved (a constant from elsewhere,
#: an env value): the route is kept with an empty endpoint — never a ``{NAME}`` placeholder.
_UNKNOWN = object()
_SENTINEL_NO_ARGS = object()
_PATH_EXPRS = frozenset({
    "string_literal", "simple_identifier", "navigation_expression", "additive_expression",
})


def string_constants(root: Node, source: bytes) -> dict[str, str | None]:
    """``val`` / ``const val`` string properties of this file → their value, by simple name
    (``object Paths { const val USERS = "/users" }`` → ``USERS``). A value may interpolate another
    constant. A name declared twice with different values, or with a value that does not
    resolve, maps to None."""
    raw: dict[str, list[Node]] = {}

    def walk(n: Node) -> None:
        if n.type == "property_declaration":
            var = next((c for c in n.named_children if c.type == "variable_declaration"), None)
            name = var.named_children[0] if var is not None and var.named_children else None
            value = next((c for c in n.named_children if c.type in _PATH_EXPRS), None)
            if name is not None and name.type == "simple_identifier" and value is not None:
                raw.setdefault(node_text(name, source), []).append(value)
        for c in n.named_children:
            walk(c)

    walk(root)
    out: dict[str, str | None] = {}

    def value_of(name: str, depth: int) -> str | None:
        if name in out:
            return out[name]
        nodes = raw.get(name)
        if not nodes or depth > 8:
            return None
        values = {_resolve(v, source, lambda n: value_of(n, depth + 1)) for v in nodes}
        out[name] = values.pop() if len(values) == 1 else None
        return out[name]

    for name in raw:
        value_of(name, 0)
    return out


def _resolve(node: Node, source: bytes, lookup: Callable[[str], str | None]) -> str | None:
    """A path expression → its literal string, or None when any part is not a known constant.
    String content is kept verbatim (Ktor path parameters are written ``{id}`` inside it)."""
    if node.type == "string_literal":
        parts: list[str] = []
        for child in node.named_children:
            if child.type == "string_content":
                parts.append(node_text(child, source))
            elif child.type in ("interpolated_identifier", "interpolated_expression"):
                expr = child if child.type == "interpolated_identifier" else (
                    child.named_children[0] if child.named_children else None)
                value = lookup(node_text(expr, source)) if expr is not None and expr.type in (
                    "interpolated_identifier", "simple_identifier") else None
                if value is None:
                    return None
                parts.append(value)
        return "".join(parts)
    if node.type == "simple_identifier":
        return lookup(node_text(node, source))
    if node.type == "navigation_expression":  # Paths.USERS → USERS
        names = [c for c in node.named_children if c.type == "simple_identifier"]
        suffix = next((c for c in node.named_children if c.type == "navigation_suffix"), None)
        last = suffix.named_children[0] if suffix is not None and suffix.named_children else None
        if not names or last is None or last.type != "simple_identifier":
            return None
        return lookup(node_text(last, source))
    if node.type == "additive_expression" and len(node.named_children) == 2:
        left, right = (_resolve(c, source, lookup) for c in node.named_children)
        return left + right if left is not None and right is not None else None
    return None


def _first_arg(call: Node, source: bytes, consts: dict[str, str | None]) -> object:
    """The first argument of a call_expression as a path: a resolved string; ``_UNKNOWN`` for a
    path expression (string, constant, ``A + "/x"``) that does not resolve; ``_SENTINEL_NO_ARGS``
    for a trailing-lambda-only call; None when there is no usable argument."""
    for suffix in call.named_children:
        if suffix.type != "call_suffix":
            continue
        val_args = next((c for c in suffix.named_children if c.type == "value_arguments"), None)
        if val_args is None:
            continue
        first_arg = next((c for c in val_args.named_children if c.type == "value_argument"), None)
        expr = next((c for c in first_arg.named_children if c.type in _PATH_EXPRS), None) \
            if first_arg is not None else None
        if expr is None:
            return None
        value = _resolve(expr, source, consts.get)
        return _UNKNOWN if value is None else value
    return _SENTINEL_NO_ARGS


def _lambda_body(call: Node) -> Node | None:
    """Return the lambda_literal body of a trailing-lambda call, if present."""
    for suffix in call.named_children:
        if suffix.type != "call_suffix":
            continue
        annotated = next(
            (c for c in suffix.named_children if c.type == "annotated_lambda"),
            None,
        )
        if annotated is None:
            continue
        return next((c for c in annotated.named_children if c.type == "lambda_literal"), None)
    return None


def _guard_name(call: Node, source: bytes) -> str:
    """``authenticate("jwt")`` → ``authenticate("jwt")``; ``authenticate { }`` → ``authenticate``
    (the default provider). The provider names are kept: they say which check runs."""
    for suffix in call.named_children:
        if suffix.type == "call_suffix":
            args = next((c for c in suffix.named_children if c.type == "value_arguments"), None)
            if args is not None and args.named_children:
                return "authenticate" + node_text(args, source)
    return "authenticate"


def _callee_name(call: Node, source: bytes) -> str | None:
    callee = call.named_children[0] if call.named_children else None
    if callee is None or callee.type != "simple_identifier":
        return None
    return node_text(callee, source)


def _collect_routes(
    node: Node,
    source: bytes,
    path: str,
    parent_id: str,
    limit: int,
    prefix: str | None = "",
    guards: tuple[str, ...] = (),
    consts: dict[str, str | None] | None = None,
) -> list[Emit]:
    """Recursively collect Ktor routes from a subtree. ``guards`` are the enclosing
    ``authenticate(...)`` blocks, outermost first."""
    consts = consts if consts is not None else {}
    out: list[Emit] = []
    for child in node.named_children:
        if child.type != "call_expression":
            out.extend(_collect_routes(child, source, path, parent_id, limit, prefix, guards, consts))
            continue

        verb = _callee_name(child, source)
        if verb is None:
            out.extend(_collect_routes(child, source, path, parent_id, limit, prefix, guards, consts))
            continue

        if verb == "authenticate":
            # Guard block: every route inside runs after this authentication check.
            body = _lambda_body(child)
            if body is not None:
                out.extend(_collect_routes(
                    body, source, path, parent_id, limit, prefix,
                    (*guards, _guard_name(child, source)), consts,
                ))
            continue

        if verb == "route":
            # Prefix block — push path segment and recurse into lambda body. An unresolved
            # segment makes every nested address unknown (prefix None).
            sub = _first_arg(child, source, consts)
            new_prefix = (None if prefix is None or sub is _UNKNOWN
                          else prefix + (sub if isinstance(sub, str) else ""))
            body = _lambda_body(child)
            if body is not None:
                out.extend(
                    _collect_routes(body, source, path, parent_id, limit, new_prefix, guards, consts)
                )
            continue

        if verb not in _HTTP_VERBS and verb not in _WS_BUILDERS:
            out.extend(_collect_routes(child, source, path, parent_id, limit, prefix, guards, consts))
            continue

        method, kind = ("WEBSOCKET", "ws") if verb in _WS_BUILDERS else (verb.upper(), "route")
        arg = _first_arg(child, source, consts)
        full_endpoint: str | None
        if arg is _SENTINEL_NO_ARGS:
            # Trailing-lambda only — e.g. `post { }` inside route("/path")
            # Endpoint is just the inherited prefix.
            if prefix == "":
                continue
            full_endpoint = prefix
        elif arg is None:
            continue
        elif arg is _UNKNOWN or prefix is None:
            full_endpoint = None  # the address exists but is not known (decision Q8)
        else:
            full_endpoint = prefix + str(arg)

        # The handler is the trailing lambda: anonymous — no name, its line is where it starts.
        body = _lambda_body(child)
        handler_line = body.start_point[0] + 1 if body is not None else None
        # The base parser's statement of this call is enriched in place (``same``) — one record
        # per route, not a base statement plus an ``…#2`` copy; else a new one on the file.
        text = node_text(child, source).split("\n", 1)[0]
        out.append(Emit(
            child,
            {"semanticType": "route", "framework": "ktor", "method": method,
             "endpoint": full_endpoint, "routeKind": kind, "handlerLine": handler_line},
            fill={"guards": list(guards) or None, "authRequired": True if guards else None},
            enrich="same", parent=parent_id, text=text[:limit] if limit else text,
        ))

    return out


def detect_ktor_routes(
    root: Node,
    source: bytes,
    file_path: str,
    file_id: str,
    limit: int = 8000,
) -> list[Emit]:
    """Every Ktor HTTP / WebSocket route of the file, for :func:`~..emits.apply`."""
    return _collect_routes(root, source, file_path, file_id, limit,
                           consts=string_constants(root, source))
