"""Vert.x event/route detection over the **Java** AST. Vert.x is call-based, so this
walks method invocations and maps them to event/messaging/route semantics:

  eventBus.send/publish/consumer → eventbus_send / eventbus_publish / eventbus_consumer
  vertx.setTimer/setPeriodic     → timer
  vertx.deployVerticle(...)      → verticle_deploy
  ServiceBinder…setAddress(...)  → service_proxy    (+ @ProxyGen interfaces)
  router.get/post/…("/path")     → route

The shared decision table (which call → which semanticType) lives in
``parsers/vertx_common``; only the Java-grammar AST extraction (``_parts``) is here. The
Groovy counterpart is ``parsers/groovy_vertx/events.py``.

Per the capture contract, a detection sets ``semanticType`` on the **same span**: where the
base parser already captured the enclosing statement (top level of a method body) we enrich
it in place; for calls inside lambda handlers — which the base skips as a nested scope — we
add a statement parented to the enclosing function. Mutates ``record``."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, SemanticType, Statement
from ..treesitter import first_line, node_text
from ..vertx_common import classify_call, enclosing_statement, owner_function, render_address


def _java_string(node: Node, source: bytes) -> str | None:
    """A Java ``string_literal`` node → its text value (Java has no string interpolation)."""
    frag = next((c for c in node.named_children if c.type == "string_fragment"), None)
    return node_text(frag, source) if frag is not None else node_text(node, source).strip('"')


def _invocations(root: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        if n.type == "method_invocation":
            out.append(n)
        for c in n.named_children:
            walk(c)

    walk(root)
    return out


def _parts(
    call: Node, source: bytes, consts: dict[str, str]
) -> tuple[str, str | None, str | None, str]:
    """(method, folded-first-arg, raw-first-arg, receiver) for a Java ``method_invocation``.
    The Java grammar exposes ``object``/``name``/``arguments`` fields directly on the call.
    The address/path is the first argument; ``consts`` folds it when it is a String literal or
    a ``static final String`` constant (``registerHandler(ADDRESS_WEB, h)`` → its value)."""
    name_node = call.child_by_field_name("name")
    method = node_text(name_node, source) if name_node is not None else ""
    obj_node = call.child_by_field_name("object")
    obj = node_text(obj_node, source) if obj_node is not None else ""
    args = call.child_by_field_name("arguments")
    first_str = first_arg = None
    if args is not None and args.named_children:
        first_node = args.named_children[0]
        first_arg = node_text(first_node, source)
        first_str = render_address(first_node, source, consts, _java_string)
    return method, first_str, first_arg, obj


def _classify(
    call: Node, source: bytes, consts: dict[str, str]
) -> tuple[SemanticType, str | None, str | None, str | None] | None:
    """→ (semanticType, method, endpoint, routeKind) or None."""
    method, first_str, first_arg, obj = _parts(call, source, consts)
    return classify_call(method, first_str, first_arg, obj)


def detect_vertx(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str] | None = None
) -> bool:
    """Enrich/add Vert.x statements on ``record``. Returns True if anything matched.
    ``consts`` (``name → value``) folds symbolic addresses to their String value."""
    matched = False
    fid = file_id(path)
    seen = {s.id for s in record.statements}
    consts = consts or {}

    for call in _invocations(root):
        info = _classify(call, source, consts)
        if info is None:
            continue
        semantic, method, endpoint, route_kind = info
        line = call.start_point[0] + 1

        stmt = enclosing_statement(line, record.statements)
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
                parentId=owner_function(line, record.functions, fid),
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
                id=new_id, parentId=cls.id, nodeType="synthetic",
                semanticType="service_proxy", text="@ProxyGen", framework="vertx",
                startLine=cls.startLine, endLine=cls.startLine, path=path,
            ))
            matched = True

    return matched
