"""Spring WebMvc.fn / WebFlux.fn *functional* routing detection (AST-based).

Annotated controllers are read off the ``FileRecord`` (:mod:`routes`), but functional
routes are declared as data inside a ``@Bean`` returning ``RouterFunction``:

    route(GET("/api/products"), handler::list)
        .andRoute(POST("/api/products").and(contentType(JSON)), handler::create);

    route().nest(accept(JSON), b -> b.GET("/api/v2/catalog", handler::list)).build();

We walk the AST for every RequestPredicate / builder verb call — ``GET`` / ``POST`` /
``PUT`` / ``PATCH`` / ``DELETE`` / ``HEAD`` / ``OPTIONS`` with a string path argument —
and emit one route each. The handler (a ``ref::method``) is captured best-effort from the
verb call itself (builder form) or the enclosing ``route`` / ``andRoute`` (static form).

A shared prefix — ``nest(path("/v2"), …)`` / ``andNest(path("/v2"), …)`` or the builder's
``.path("/v2", b -> …)`` — is joined onto every route nested inside it (nests compose). A
prefix whose value is not a string literal makes the address unknown: the route's endpoint is
empty (non-path predicates such as ``accept(JSON)`` add nothing).

Gated by the caller on a ``RouterFunction`` signature so a stray ``GET(...)`` elsewhere
cannot match.
"""

from __future__ import annotations

from typing import Any

from tree_sitter import Node

from ...schemas import FileRecord
from ..emits import Emit
from ..handler_sites import handler_location, locate_method
from ..treesitter import node_text

_FN_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_ROUTE_CALLS = {"route", "andRoute", "andNest"}
_NEST_CALLS = {"nest", "andNest"}

#: Sentinel for a prefix that exists but is not a string literal.
_UNRESOLVED = object()


def _path_prefix(predicate: Node, source: bytes) -> object | None:
    """The path prefix of a ``nest`` predicate: the argument of a ``path(...)`` call inside it
    (``path("/v2")``, ``path("/v2").and(accept(JSON))``) → the string, ``_UNRESOLVED`` when it
    is not a literal, None when the predicate has no ``path(...)``."""
    stack = [predicate]
    while stack:
        n = stack.pop()
        if n.type == "method_invocation":
            nm = n.child_by_field_name("name")
            if nm is not None and node_text(nm, source) == "path":
                p = _first_string_arg(n.child_by_field_name("arguments"), source)
                return p if p is not None else _UNRESOLVED
        stack.extend(reversed(n.named_children))
    return None


def _nest_prefix(call: Node, source: bytes) -> str | None:
    """The joined path prefix of the ``nest`` / ``andNest`` / builder ``.path`` calls that
    enclose ``call`` (outermost first), "" when there are none, None when one is unresolved."""
    prefixes: list[str] = []
    node = call
    while (parent := node.parent) is not None:
        inv = parent.parent if parent.type == "argument_list" else None
        if inv is not None and inv.type == "method_invocation" and parent.named_children \
                and node != parent.named_children[0]:  # nested in a later argument
            nm = inv.child_by_field_name("name")
            name = node_text(nm, source) if nm is not None else ""
            first = parent.named_children[0]
            prefix: object | None = None
            if name in _NEST_CALLS:
                prefix = _path_prefix(first, source)
            elif name == "path":  # builder form: .path("/v2", b -> b.GET(...))
                p = _first_string_arg(parent, source)
                prefix = p if p is not None else _UNRESOLVED
            if prefix is _UNRESOLVED:
                return None
            if isinstance(prefix, str):
                prefixes.append(prefix)
        node = parent
    return "".join("/" + p.strip("/") for p in reversed(prefixes) if p.strip("/"))


def _first_string_arg(args: Node | None, source: bytes) -> str | None:
    if args is None:
        return None
    for a in args.named_children:
        if a.type == "string_literal":
            frag = next((c for c in a.named_children if c.type == "string_fragment"), None)
            return node_text(frag, source) if frag is not None else node_text(a, source).strip('"')
    return None


def _join(prefix: str, sub: str) -> str:
    parts = [x.strip("/") for x in (prefix, sub) if x and x.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _ref_name(node: Node, source: bytes) -> str:
    return node_text(node, source).rsplit("::", 1)[-1].strip()


_HANDLER_ARGS = ("method_reference", "lambda_expression")


def _handler_arg(call: Node, args: Node | None, source: bytes) -> Node | None:
    """The handler argument of a functional route: a method reference (``handler::list``) or a
    lambda, either on the verb call (builder ``.GET("/x", handler::list)``) or on the enclosing
    ``route`` / ``andRoute`` (static ``route(GET("/x"), handler::list)``)."""
    if args is not None:
        for a in args.named_children:
            if a.type in _HANDLER_ARGS:
                return a
    node, depth = call.parent, 0
    while node is not None and depth < 8:
        if node.type == "method_invocation":
            nm = node.child_by_field_name("name")
            if nm is not None and node_text(nm, source) in _ROUTE_CALLS:
                pa = node.child_by_field_name("arguments")
                for a in pa.named_children if pa is not None else []:
                    if a.type in _HANDLER_ARGS:
                        return a
        node, depth = node.parent, depth + 1
    return None


def _receiver_type(ref: Node, receiver: str, source: bytes) -> str | None:
    """The declared type of a method reference's receiver: ``h`` in ``h::list`` is a parameter
    of the enclosing method or a field of the class (``ProductHandler h``); ``ProductHandler``
    in ``ProductHandler::list`` is the type itself."""
    if receiver[:1].isupper() and receiver.replace(".", "").isidentifier():
        return receiver
    node: Node | None = ref.parent
    while node is not None:
        if node.type in ("method_declaration", "constructor_declaration"):
            params = node.child_by_field_name("parameters")
            for p in params.named_children if params is not None else []:
                nm, typ = p.child_by_field_name("name"), p.child_by_field_name("type")
                if nm is not None and typ is not None and node_text(nm, source) == receiver:
                    return node_text(typ, source)
        elif node.type == "class_body":
            for member in node.named_children:
                if member.type != "field_declaration":
                    continue
                typ = member.child_by_field_name("type")
                for d in member.named_children:
                    nm = d.child_by_field_name("name") if d.type == "variable_declarator" else None
                    if nm is not None and typ is not None and node_text(nm, source) == receiver:
                        return node_text(typ, source)
        node = node.parent
    return None


def _handler_for(
    call: Node, args: Node | None, source: bytes, path: str, methods: dict[str, int | None],
    index: Any | None = None,
) -> tuple[str | None, int | None, str | None]:
    """(handler, handlerLine, handlerPath). A method reference names the handler: ``this::m`` is
    a method of this file; ``h::list`` a method of the receiver's class, found through the index
    (``java.method-lines``) when declared elsewhere. A lambda has no name; its line is where it
    starts."""
    arg = _handler_arg(call, args, source)
    if arg is None:
        return None, None, None
    if arg.type == "lambda_expression":
        return None, arg.start_point[0] + 1, None
    name = _ref_name(arg, source)
    receiver = node_text(arg, source).rsplit("::", 1)[0].strip()
    if receiver == "this":
        return name, methods.get(name), None
    site = locate_method(index, "java", _receiver_type(arg, receiver, source), name)
    return (name, *handler_location(site, path))


def detect_spring_functional_routes(
    root: Node, source: bytes, path: str, record: FileRecord, index: Any | None = None
) -> list[Emit]:
    routes: list[Emit] = []
    methods: dict[str, int | None] = {}  # method name → declaration line (declared twice → None)
    for f in record.functions:
        methods[f.name] = f.startLine if f.name not in methods else None

    def visit(n: Node) -> None:
        if n.type == "method_invocation":
            name_node = n.child_by_field_name("name")
            name = node_text(name_node, source) if name_node is not None else ""
            if name in _FN_VERBS:
                args = n.child_by_field_name("arguments")
                p = _first_string_arg(args, source)
                if p is not None:
                    handler, handler_line, handler_path = _handler_for(
                        n, args, source, path, methods, index)
                    prefix = _nest_prefix(n, source)
                    routes.append(
                        Emit(n, dict(
                            semanticType="route",
                            method=name,
                            endpoint=_join(prefix, p) if prefix is not None else None,
                            framework="spring",
                            handler=handler,
                            handlerLine=handler_line,
                            handlerPath=handler_path,
                            routeKind="route",
                            isRegex=False,
                        ), parent=record.id)
                    )
        for c in n.named_children:
            visit(c)

    visit(root)
    return routes
