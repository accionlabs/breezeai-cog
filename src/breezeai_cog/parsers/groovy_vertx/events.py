"""Vert.x detection over the dekobon **Groovy** AST. Mirrors ``java_vertx/events.py`` but
for Groovy's call shape: a ``method_invocation``'s callee is the ``function`` field — a bare
``identifier`` (no receiver) or a ``field_access`` (``receiver.method``) — and route paths
are GStrings (``"${prefix}/:id"``) rendered here to ``{prefix}/:id``.

Handles both ``RouteMatcher`` idioms:

  route.get("${prefix}/x", handler)                              # receiver-qualified
  def rm = new RouteMatcher(); rm.with { get("${p}/x", h) }      # bare, inside a scope

The shared semantic decision table lives in ``parsers/vertx_common``; only the Groovy AST
extraction (``_parts`` / ``_render_path``) and the bare-call ``route_scope`` inference are
here. Mutates ``record``; returns True if anything matched."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..enclosing import Enclosing
from ..treesitter import first_line, node_text
from ..vertx_common import (
    HANDLER_CALLS,
    HANDLER_SEMANTICS,
    classify_call,
    handler_info,
    method_lines,
    render_address,
)


def _invocations(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "method_invocation":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _render_path(node: Node, source: bytes) -> str | None:
    """Render a Groovy ``string_literal`` (possibly a GString) to a path, interpolations
    collapsed to ``{name}`` placeholders: ``"${prefix}/:platformId/empno"`` →
    ``{prefix}/:platformId/empno``; ``"${config.'path'}/:filterId"`` → ``{config.path}/:filterId``."""
    if node.type != "string_literal":
        return None
    parts: list[str] = []
    for c in node.named_children:
        if c.type == "string_fragment":
            parts.append(node_text(c, source))
        elif "interpolation" in c.type:  # gstring_brace_interpolation / gstring_interpolation
            inner = node_text(c, source).strip()
            if inner.startswith("${") and inner.endswith("}"):
                inner = inner[2:-1]
            elif inner.startswith("$"):
                inner = inner[1:]
            inner = inner.replace("'", "").replace('"', "").strip()
            parts.append("{" + inner + "}")
    if parts:
        return "".join(parts) or None
    text = node_text(node, source).strip("\"'")  # plain literal, no child fragments
    return text or None


def _parts(
    call: Node, source: bytes, consts: dict[str, str]
) -> tuple[str, str | None, str | None, str]:
    """(method, rendered-path, raw-first-arg, receiver) for a Groovy ``method_invocation``.
    The dekobon grammar puts the callee in the ``function`` field: a bare ``identifier``
    (receiver ``""``) or a ``field_access`` (``obj.method``). The address/path is the first
    argument: a string literal renders as a (GString-aware) path; otherwise ``consts`` folds a
    ``static final String`` constant address to its value."""
    fn = call.child_by_field_name("function")
    method = obj = ""
    if fn is not None:
        if fn.type == "field_access":
            obj_node = fn.child_by_field_name("object")
            field = fn.child_by_field_name("field")
            obj = node_text(obj_node, source) if obj_node is not None else ""
            method = node_text(field, source) if field is not None else ""
        else:
            method = node_text(fn, source)
    args = call.child_by_field_name("arguments")
    path = first_arg = None
    if args is not None and args.named_children:
        first_node = args.named_children[0]
        first_arg = node_text(first_node, source)
        path = render_address(first_node, source, consts, _render_path)
    return method, path, first_arg, obj


def _handler_arg(call: Node, source: bytes) -> Node | None:
    """The handler argument of a route / consumer call: the second argument when the call takes
    the address and the handler (``eb.consumer("a", { m -> … })``, 2.x ``rm.get("/x", h)``; a
    third, 2.x ``registerHandler`` completion handler, is not it), else the argument of the last
    chained ``.handler(…)``. In this grammar a chained call is
    ``method_invocation(function: field_access(object: <call>, field: handler))``."""
    args = call.child_by_field_name("arguments")
    named = list(args.named_children) if args is not None else []
    found = named[1] if len(named) >= 2 else None
    node = call
    while (access := node.parent) is not None and access.type == "field_access" \
            and access.child_by_field_name("object") == node \
            and (parent := access.parent) is not None and parent.type == "method_invocation":
        field = access.child_by_field_name("field")
        if field is not None and node_text(field, source) in HANDLER_CALLS:
            pargs = parent.child_by_field_name("arguments")
            found = pargs.named_children[0] if pargs is not None and pargs.named_children else None
        node = parent
    return found


def detect_vertx_groovy(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str] | None = None
) -> bool:
    """Enrich/add Vert.x statements on ``record``. Returns True if anything matched.
    ``consts`` (``name → value``) folds symbolic addresses to their String value."""
    matched = False
    fid = file_id(path)
    seen = {s.id for s in record.statements}
    consts = consts or {}
    # Vert.x 2.x RouteMatcher: bare `get("/x", h)` calls (no receiver) inside `rm.with {}`
    # are routes only when the file actually builds a RouteMatcher — a cheap file-level gate
    # that keeps a stray `map.get(...)` from being mistaken for a route.
    uses_routematcher = b"RouteMatcher" in source
    enclosing = Enclosing(record)
    methods = method_lines(record.functions)

    for call in _invocations(root):
        method, endpoint_path, first_arg, obj = _parts(call, source, consts)
        route_scope = bool(
            uses_routematcher and obj == "" and endpoint_path and "/" in endpoint_path
        )
        info = classify_call(method, endpoint_path, first_arg, obj, route_scope=route_scope)
        if info is None:
            continue
        semantic, verb, endpoint, route_kind = info
        line = call.start_point[0] + 1
        handler, handler_line = (
            handler_info(_handler_arg(call, source), source, methods)
            if semantic in HANDLER_SEMANTICS else (None, None)
        )

        stmt = enclosing.statement(line)
        if stmt is not None:  # detection on the same span → enrich in place
            stmt.semanticType = semantic
            stmt.framework = "vertx"
            if verb:
                stmt.method = verb
            if endpoint:
                stmt.endpoint = endpoint
            if route_kind:
                stmt.routeKind = route_kind
            if handler:
                stmt.handler = handler
            if handler_line:
                stmt.handlerLine = handler_line
        else:  # inside a closure the base skipped → add a statement to the enclosing function
            new_id = disambiguate(statement_id(path, line, call.start_point[1]), seen)
            record.statements.append(Statement(
                id=new_id,
                parentId=enclosing.function_id(line, fid),
                nodeType=call.type,
                semanticType=semantic,
                text=first_line(node_text(call, source)),
                method=verb,
                endpoint=endpoint,
                framework="vertx",
                routeKind=route_kind,
                handler=handler,
                handlerLine=handler_line,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            ))
        matched = True

    return matched
