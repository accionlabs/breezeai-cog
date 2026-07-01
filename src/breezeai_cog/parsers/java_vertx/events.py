"""Vert.x event/route detection (spec A4 'Vert.x family'). Vert.x is call-based, so this
walks the Java AST for method invocations and maps them to event/messaging semantics:

  eventBus.send/publish/consumer → eventbus_send / eventbus_publish / eventbus_consumer
  vertx.setTimer/setPeriodic     → timer
  vertx.deployVerticle(...)      → verticle_deploy
  ServiceBinder…setAddress(...)  → service_proxy    (+ @ProxyGen interfaces)
  router.get/post/…("/path")     → route

Per the contract (Part C / B1.4), a detection sets ``semanticType`` on the **same span**:
so where the base parser already captured the enclosing statement (top level of a method
body) we enrich it in place; for calls inside lambda handlers — which the base skips as a
nested scope — we add a statement parented to the enclosing function. Mutates ``record``."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..treesitter import first_line, node_text

_EVENTBUS = {
    "send": "eventbus_send",
    "publish": "eventbus_publish",
    "consumer": "eventbus_consumer",
    "localConsumer": "eventbus_consumer",
}
_TIMERS = {"setTimer", "setPeriodic"}
_HTTP_VERBS = {"get", "post", "put", "delete", "patch", "options", "head"}


def _invocations(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "method_invocation":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _parts(call: Node, source: bytes) -> tuple[str, str | None, str | None, str]:
    name_node = call.child_by_field_name("name")
    method = node_text(name_node, source) if name_node is not None else ""
    obj_node = call.child_by_field_name("object")
    obj = node_text(obj_node, source) if obj_node is not None else ""
    args = call.child_by_field_name("arguments")
    first_str = first_arg = None
    if args is not None and args.named_children:
        first_arg = node_text(args.named_children[0], source)
        for a in args.named_children:
            if a.type == "string_literal":
                first_str = node_text(a, source).strip('"')
                break
    return method, first_str, first_arg, obj


def _classify(call: Node, source: bytes) -> tuple[str, str | None, str | None, str | None] | None:
    """→ (semanticType, method, endpoint, routeKind) or None."""
    method, first_str, first_arg, obj = _parts(call, source)
    obj_l = obj.lower()

    if method in _EVENTBUS and first_str is not None and ("bus" in obj_l or obj_l == "eb"):
        return _EVENTBUS[method], None, first_str, None
    if method in _TIMERS:
        return "timer", None, None, None
    if method == "deployVerticle":
        return "verticle_deploy", None, first_str or first_arg, None
    if method == "setAddress" and first_str is not None:
        return "service_proxy", None, first_str, None
    if method in _HTTP_VERBS and first_str and first_str.startswith("/") and "router" in obj_l:
        return "route", method.upper(), first_str, "route"
    if method == "route" and first_str and first_str.startswith("/") and "router" in obj_l:
        return "route", None, first_str, "route"
    return None


def _enclosing_statement(line: int, statements: list[Statement]) -> Statement | None:
    best: Statement | None = None
    best_span: int | None = None
    for s in statements:
        if s.startLine <= line <= s.endLine:
            span = s.endLine - s.startLine
            if best_span is None or span < best_span:
                best, best_span = s, span
    return best


def _owner_function(line: int, functions, fallback: str) -> str:
    best = None
    best_span: int | None = None
    for f in functions:
        if f.startLine <= line <= f.endLine:
            span = f.endLine - f.startLine
            if best_span is None or span < best_span:
                best, best_span = f, span
    return best.id if best is not None else fallback


def detect_vertx(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Enrich/add Vert.x statements on ``record``. Returns True if anything matched."""
    matched = False
    fid = file_id(path)
    seen = {s.id for s in record.statements}

    for call in _invocations(root):
        info = _classify(call, source)
        if info is None:
            continue
        semantic, method, endpoint, route_kind = info
        line = call.start_point[0] + 1

        stmt = _enclosing_statement(line, record.statements)
        if stmt is not None:  # detection on the same span → enrich in place
            stmt.semanticType = semantic
            stmt.framework = "vertx"
            if method:
                stmt.method = method
            if endpoint:
                stmt.endpoint = endpoint
            if route_kind:
                stmt.routeKind = route_kind
        else:  # inside a lambda (base skips nested scopes) → add a statement
            new_id = disambiguate(statement_id(path, line, call.start_point[1]), seen)
            record.statements.append(Statement(
                id=new_id,
                parentId=_owner_function(line, record.functions, fid),
                nodeType=call.type,
                semanticType=semantic,
                text=first_line(node_text(call, source)),
                method=method,
                endpoint=endpoint,
                framework="vertx",
                routeKind=route_kind,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            ))
        matched = True

    for cls in record.classes:  # @ProxyGen service interfaces
        if any(d.name == "ProxyGen" for d in cls.decorators):
            new_id = disambiguate(statement_id(path, cls.startLine, 0), seen)
            record.statements.append(Statement(
                id=new_id, parentId=cls.id, nodeType="annotation",
                semanticType="service_proxy", text="@ProxyGen", framework="vertx",
                startLine=cls.startLine, endLine=cls.startLine, path=path,
            ))
            matched = True

    return matched
