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

from collections.abc import Callable, Mapping

from tree_sitter import Node

from ..addresses import join_runtime, runtime_part, with_literal
from ..emits import Emit
from ..kotlin.constants import PATH_EXPRS, ConstantScope, resolve_expr, string_constants
from ..treesitter import node_text


_HTTP_VERBS = frozenset({"get", "post", "put", "delete", "patch", "head", "options"})
#: ``webSocket("/ws") { … }`` / ``webSocketRaw(…)``: a WebSocket upgrade (method ``WEBSOCKET``,
#: ``routeKind: ws``). A client session is ``client.webSocket(…)`` — a member call, never matched.
_WS_BUILDERS = frozenset({"webSocket", "webSocketRaw"})


#: An argument that is a path expression whose value cannot be resolved (a constant not visible
#: from this file, an env value): the route is kept with an empty endpoint — never a ``{NAME}`` placeholder.
_UNKNOWN = object()
_SENTINEL_NO_ARGS = object()
def _first_arg(call: Node, source: bytes, lookup: Callable[[str], str | None]) -> object:
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
        expr = next((c for c in first_arg.named_children if c.type in PATH_EXPRS), None) \
            if first_arg is not None else None
        if expr is None:
            return None
        value = resolve_expr(expr, source, lookup)
        return _UNKNOWN if value is None else value
    return _SENTINEL_NO_ARGS


def _first_arg_node(call: Node) -> Node | None:
    """A call's first argument expression (``"${env.base}/x"``, ``prefixFromEnv()``)."""
    for suffix in call.named_children:
        if suffix.type != "call_suffix":
            continue
        val_args = next((c for c in suffix.named_children if c.type == "value_arguments"), None)
        first = next((c for c in val_args.named_children if c.type == "value_argument"), None) \
            if val_args is not None else None
        if first is not None:
            return first.named_children[-1] if first.named_children else first
    return None


def _path_template(call: Node, source: bytes, lookup: Callable[[str], str | None]) -> str:
    """A route path argument with the parts read at runtime marked (addresses.py): literal text
    and constants as their value, anything else as ``${name}`` (``"${env.base}/x"`` →
    ``${base}/x``, ``prefixFromEnv()`` → ``${prefixFromEnv}``). Ktor's own ``{id}`` parameters
    are string text and stay as written."""
    def render(node: Node) -> str:
        value = resolve_expr(node, source, lookup) if node.type in PATH_EXPRS else None
        if value is None and node.type == "interpolated_identifier":
            value = lookup(node_text(node, source))  # `$API`: a constant by name
        if value is not None:
            return value
        if node.type == "string_literal":
            out: list[str] = []
            for child in node.named_children:
                if child.type == "string_content":
                    out.append(node_text(child, source))
                elif child.type in ("interpolated_expression", "interpolated_identifier"):
                    out.append(render(child))  # `${env.base}` / `$API`
                # the `${` / `$` / `}` delimiters carry no text of the path
            return "".join(out)
        if node.type == "additive_expression" and len(node.named_children) == 2:
            return render(node.named_children[0]) + render(node.named_children[1])
        return runtime_part(node_text(node, source))

    first = _first_arg_node(call)
    return render(first) if first is not None else runtime_part("")


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


def _requires_auth(call: Node, source: bytes) -> bool:
    """Whether an ``authenticate(...)`` block certainly rejects an unauthenticated call. Not when
    it is optional (``optional = true``, ``strategy = AuthenticationStrategy.Optional``) or the
    option cannot be read (``optional = flag``): anonymous calls may get through."""
    for suffix in call.named_children:
        if suffix.type != "call_suffix":
            continue
        args = next((c for c in suffix.named_children if c.type == "value_arguments"), None)
        for arg in args.named_children if args is not None else []:
            parts = arg.named_children
            if len(parts) < 2 or parts[0].type != "simple_identifier":
                continue  # a provider name, positional
            name, value = node_text(parts[0], source), node_text(parts[-1], source).strip()
            if name == "optional" and value != "false":
                return False
            if name == "strategy" and not value.endswith(("Required", "FirstSuccessful")):
                return False
    return True


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
    consts: Callable[[str], str | None] | None = None,
    required: bool = False,
) -> list[Emit]:
    """Recursively collect Ktor routes from a subtree. ``guards`` are the enclosing
    ``authenticate(...)`` blocks, outermost first; ``required``: one of them certainly rejects an
    unauthenticated call (:func:`_requires_auth`) — only then is ``authRequired`` set. ``consts``
    resolves a constant reference (a :class:`~..kotlin.constants.ConstantScope`)."""
    consts = consts if consts is not None else (lambda _ref: None)
    out: list[Emit] = []
    for child in node.named_children:
        if child.type != "call_expression":
            out.extend(_collect_routes(child, source, path, parent_id, limit, prefix, guards, consts, required))
            continue

        verb = _callee_name(child, source)
        if verb is None:
            out.extend(_collect_routes(child, source, path, parent_id, limit, prefix, guards, consts, required))
            continue

        if verb == "authenticate":
            # Guard block: every route inside runs after this authentication check.
            body = _lambda_body(child)
            if body is not None:
                out.extend(_collect_routes(
                    body, source, path, parent_id, limit, prefix,
                    (*guards, _guard_name(child, source)), consts,
                    required or _requires_auth(child, source),
                ))
            continue

        if verb == "route":
            # Prefix block — push path segment and recurse into lambda body. An unresolved
            # segment — a path expression that does not resolve, or an argument that is not one
            # (``route(prefixFromEnv())``) — is a value read at runtime: a ``${name}`` part of
            # every nested address (addresses.py).
            sub = _first_arg(child, source, consts)
            new_prefix: str | None
            if prefix is None:
                new_prefix = None
            elif sub is _UNKNOWN or sub is None:
                part = _path_template(child, source, consts)
                new_prefix = join_runtime(prefix, part) if prefix.strip("/") else part
            else:
                new_prefix = prefix + (sub if isinstance(sub, str) else "")
            body = _lambda_body(child)
            if body is not None:
                out.extend(
                    _collect_routes(body, source, path, parent_id, limit, new_prefix, guards, consts, required)
                )
            continue

        if verb not in _HTTP_VERBS and verb not in _WS_BUILDERS:
            out.extend(_collect_routes(child, source, path, parent_id, limit, prefix, guards, consts, required))
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
        elif prefix is None:
            full_endpoint = None  # the address exists but is not known (decision Q8)
        elif arg is _UNKNOWN:
            # A path with a part read at runtime: marked `${name}` (addresses.py); none literal
            # left (`get(Config.EXTERNAL)`) → unknown.
            full_endpoint = with_literal(prefix + _path_template(child, source, consts))
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
            fill={"guards": list(guards) or None, "authRequired": True if required else None},
            enrich="same", parent=parent_id, text=text[:limit] if limit else text,
        ))

    return out


def detect_ktor_routes(
    root: Node,
    source: bytes,
    file_path: str,
    file_id: str,
    limit: int = 8000,
    repo_consts: Mapping[str, str] | None = None,
) -> list[Emit]:
    """Every Ktor HTTP / WebSocket route of the file, for :func:`~..emits.apply`. A path constant
    resolves from this file, else through its imports or package in ``repo_consts`` (the
    repo-wide ``KotlinIndex.consts``)."""
    scope = ConstantScope(root, source, string_constants(root, source), repo_consts)
    return _collect_routes(root, source, file_path, file_id, limit, consts=scope)
