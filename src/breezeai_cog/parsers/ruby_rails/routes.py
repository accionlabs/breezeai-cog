"""Conservative Rails route detection over the Ruby AST."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..treesitter import first_line, node_text

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "match"}


def _calls(root: Node):
    if root.type == "call":
        yield root
    for child in root.named_children:
        yield from _calls(child)


def _method(call: Node, source: bytes) -> str | None:
    node = call.child_by_field_name("method")
    return node_text(node, source) if node is not None and node.type == "identifier" else None


def _string_value(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type != "string":
        return None
    content = next((child for child in node.named_children if child.type == "string_content"), None)
    return node_text(content, source) if content is not None else ""


def _inside_routes_draw(call: Node, source: bytes) -> bool:
    current = call.parent
    while current is not None:
        if current.type == "call" and _method(current, source) == "draw":
            receiver = current.child_by_field_name("receiver")
            if receiver is not None and node_text(receiver, source).endswith("routes"):
                return True
        current = current.parent
    return False


def _arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name("arguments")
    return list(arguments.named_children) if arguments is not None else []


def _handler(arguments: list[Node], source: bytes) -> tuple[str | None, int | None]:
    for argument in arguments[1:]:
        if argument.type == "pair":
            key = argument.child_by_field_name("key")
            value = argument.child_by_field_name("value")
            if key is not None and node_text(key, source).rstrip(":") == "to":
                if value is not None and value.type == "string":
                    return _string_value(value, source), value.start_point[0] + 1
                return None, None
    return None, None


def detect_rails_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Append explicit Rails route DSL statements; unresolved values stay null."""
    seen = {statement.id for statement in record.statements}
    matched = False
    fallback = file_id(path)

    for call in _calls(root):
        method = _method(call, source)
        if method not in _HTTP_METHODS or not _inside_routes_draw(call, source):
            continue
        args = _arguments(call)
        endpoint = _string_value(args[0], source) if args else None
        handler, handler_line = _handler(args, source)
        line = call.start_point[0] + 1
        record.statements.append(
            Statement(
                id=disambiguate(statement_id(path, line, call.start_point[1]), seen),
                parentId=fallback,
                nodeType="call",
                semanticType="route",
                text=first_line(node_text(call, source)),
                method=method.upper() if method != "match" else None,
                endpoint=endpoint,
                handler=handler,
                handlerLine=handler_line,
                framework="rails",
                routeKind="route",
                isRegex=False,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            )
        )
        matched = True
    return matched
