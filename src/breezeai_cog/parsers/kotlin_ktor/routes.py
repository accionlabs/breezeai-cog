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

import re
from collections.abc import Callable, Iterable

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


_CONTAINERS = frozenset({"object_declaration", "class_declaration", "companion_object"})


def _qualified_matches(declared: Iterable[str], ref: str) -> list[str]:
    """The declared qualified names (``Other.ITEMS``) a written one (``Admin.ITEMS``) can be: one
    chain ends with the other — ``Routes.Admin.ITEMS`` for ``Admin.ITEMS`` (the written chain
    starts lower), ``Admin.ITEMS`` for ``com.x.Admin.ITEMS`` (a package in front)."""
    return [k for k in declared
            if k == ref or k.endswith("." + ref) or ref.endswith("." + k)]


def string_constants(root: Node, source: bytes) -> dict[str, str | None]:
    """``val`` / ``const val`` string properties of this file → their value, by simple name
    (``USERS``) and, for one inside an ``object`` / class / ``companion object``, also by its
    container chain (``object Paths { const val USERS = "/users" }`` → ``Paths.USERS``). A value
    may interpolate another constant. A name declared twice with different values, or with a
    value that does not resolve, maps to None. Look names up with :func:`lookup_constant`."""
    raw: dict[str, list[Node]] = {}

    def walk(n: Node, chain: tuple[str, ...]) -> None:
        if n.type == "property_declaration":
            var = next((c for c in n.named_children if c.type == "variable_declaration"), None)
            name = var.named_children[0] if var is not None and var.named_children else None
            value = next((c for c in n.named_children if c.type in _PATH_EXPRS), None)
            if name is not None and name.type == "simple_identifier" and value is not None:
                simple = node_text(name, source)
                raw.setdefault(simple, []).append(value)
                if chain:
                    raw.setdefault(".".join((*chain, simple)), []).append(value)
            return  # a property's initializer declares no constants of the container
        if n.type in _CONTAINERS:
            nm = next((c for c in n.named_children if c.type == "type_identifier"), None)
            if n.type != "companion_object":  # a companion's members are the class's
                if nm is None:
                    return
                chain = (*chain, node_text(nm, source))
        elif n.type == "infix_expression" and [c.type for c in n.named_children] == [
                "object_literal", "simple_identifier", "lambda_literal"]:
            # the grammar's parse of a one-line ``object Paths { … }``
            chain = (*chain, node_text(n.named_children[1], source))
        for c in n.named_children:
            walk(c, chain)

    walk(root, ())
    out: dict[str, str | None] = {}

    def value_of(name: str, depth: int) -> str | None:
        if name in out:
            return out[name]
        if "." in name and name not in raw:
            return lookup_constant(
                {k: value_of(k, depth + 1) for k in _qualified_matches(
                    [k for k in raw if "." in k], name)}, name)
        nodes = raw.get(name)
        if not nodes or depth > 8:
            return None
        values = {_resolve(v, source, lambda n: value_of(n, depth + 1)) for v in nodes}
        out[name] = values.pop() if len(values) == 1 else None
        return out[name]

    for name in raw:
        value_of(name, 0)
    return out


def lookup_constant(consts: dict[str, str | None], ref: str) -> str | None:
    """A written constant reference's value: a bare name by its simple name; a qualified one
    (``Admin.ITEMS``) only through a declared container chain it matches
    (:func:`_qualified_matches`) — never another container's constant of the same simple name.
    None when unknown or when the matches disagree."""
    if "." not in ref:
        return consts.get(ref)
    values = {consts.get(k) for k in _qualified_matches([k for k in consts if "." in k], ref)}
    return values.pop() if len(values) == 1 else None


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
    if node.type == "navigation_expression":  # Paths.USERS — by its chain (lookup_constant)
        ref = "".join(node_text(node, source).split())
        return lookup(ref) if re.fullmatch(r"\w+(\.\w+)+", ref) else None
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
        value = _resolve(expr, source, lambda ref: lookup_constant(consts, ref))
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
    consts: dict[str, str | None] | None = None,
    required: bool = False,
) -> list[Emit]:
    """Recursively collect Ktor routes from a subtree. ``guards`` are the enclosing
    ``authenticate(...)`` blocks, outermost first; ``required``: one of them certainly rejects an
    unauthenticated call (:func:`_requires_auth`) — only then is ``authRequired`` set."""
    consts = consts if consts is not None else {}
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
            # (``route(prefixFromEnv())``) — makes every nested address unknown (prefix None).
            sub = _first_arg(child, source, consts)
            new_prefix = (None if prefix is None or sub is _UNKNOWN or sub is None
                          else prefix + (sub if isinstance(sub, str) else ""))
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
) -> list[Emit]:
    """Every Ktor HTTP / WebSocket route of the file, for :func:`~..emits.apply`."""
    return _collect_routes(root, source, file_path, file_id, limit,
                           consts=string_constants(root, source))
