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

Gated by the caller on a ``RouterFunction`` signature so a stray ``GET(...)`` elsewhere
cannot match. Not yet handled: a ``nest(path("/prefix"), ...)`` shared path prefix — the
nested routes are captured with their own literal path only.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..treesitter import first_line, node_text

_FN_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_ROUTE_CALLS = {"route", "andRoute", "andNest"}


def _first_string_arg(args: Node | None, source: bytes) -> str | None:
    if args is None:
        return None
    for a in args.named_children:
        if a.type == "string_literal":
            frag = next((c for c in a.named_children if c.type == "string_fragment"), None)
            return node_text(frag, source) if frag is not None else node_text(a, source).strip('"')
    return None


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


def _handler_for(
    call: Node, args: Node | None, source: bytes, methods: dict[str, int | None]
) -> tuple[str | None, int | None]:
    """(handler, handlerLine). A method reference names the handler; its line is known only for
    ``this::m`` with ``m`` declared once in this file (``h::list`` is a method of another class).
    A lambda has no name; its line is where it starts."""
    arg = _handler_arg(call, args, source)
    if arg is None:
        return None, None
    if arg.type == "lambda_expression":
        return None, arg.start_point[0] + 1
    name = _ref_name(arg, source)
    receiver = node_text(arg, source).rsplit("::", 1)[0].strip()
    return name, methods.get(name) if receiver == "this" else None


def detect_spring_functional_routes(
    root: Node, source: bytes, path: str, record: FileRecord
) -> list[Statement]:
    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
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
                    sl, sc = n.start_point[0] + 1, n.start_point[1]
                    handler, handler_line = _handler_for(n, args, source, methods)
                    routes.append(
                        Statement(
                            id=disambiguate(statement_id(path, sl, sc), seen),
                            parentId=record.id,
                            nodeType="method_invocation",
                            semanticType="route",
                            text=first_line(node_text(n, source)),
                            method=name,
                            endpoint="/" + p.strip("/") if p.strip("/") else "/",
                            framework="spring",
                            handler=handler,
                            handlerLine=handler_line,
                            routeKind="route",
                            isRegex=False,
                            startLine=sl,
                            endLine=n.end_point[0] + 1,
                            path=path,
                        )
                    )
        for c in n.named_children:
            visit(c)

    visit(root)
    return routes
