"""Shared, PHP-AST-only helpers for the PHP server-framework route detectors."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ..emit import disambiguate, statement_id
from ..schemas import Statement
from .treesitter import node_text

PHP_CALL_TYPES = {
    "function_call_expression",
    "member_call_expression",
    "nullsafe_member_call_expression",
    "scoped_call_expression",
}


def walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from walk(child)


def unwrap(node: Node) -> Node:
    if node.type == "argument" and len(node.named_children) == 1:
        return node.named_children[0]
    return node


def args_of(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return list(args.named_children) if args is not None else []


def method_of(call: Node, source: bytes) -> str | None:
    name = call.child_by_field_name("name")
    return node_text(name, source).lower() if name is not None else None


def string_value(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    node = unwrap(node)
    if node.type != "string":
        return None
    if any(child.type != "string_content" for child in node.named_children):
        return None
    return "".join(node_text(child, source) for child in node.named_children)


def strings_in_array(node: Node | None, source: bytes) -> list[str]:
    if node is None:
        return []
    node = unwrap(node)
    if node.type != "array_creation_expression":
        value = string_value(node, source)
        return [value] if value is not None else []
    values: list[str] = []
    for element in node.named_children:
        value_node = next(iter(element.named_children), None)
        value = string_value(value_node, source)
        if value is not None:
            values.append(value)
    return values


def normalize_endpoint(path: str) -> str:
    parts = [part.strip("/") for part in path.split("/") if part.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def join_endpoint(prefix: str, path: str) -> str:
    return normalize_endpoint(f"{prefix}/{path}")


def route_handler(node: Node | None, source: bytes) -> tuple[str | None, int | None]:
    if node is None:
        return None, None
    node = unwrap(node)
    literal = string_value(node, source)
    if literal is not None:
        if "::" in literal:
            cls, method = literal.rsplit("::", 1)
            return f"{cls}@{method}", node.start_point[0] + 1
        return literal, node.start_point[0] + 1
    if node.type != "array_creation_expression":
        return None, None
    elements = list(node.named_children)
    if len(elements) < 2:
        return None, None
    values = [next(iter(e.named_children), None) for e in elements]
    method_name = string_value(values[1], source)
    if method_name is None:
        return None, None
    target = values[0]
    class_name: str | None = None
    if target is not None and target.type == "class_constant_access_expression":
        class_name = node_text(target, source).rsplit("::", 1)[0]
    return (f"{class_name}@{method_name}" if class_name else method_name), node.start_point[0] + 1


def enrich_route(
    call: Node,
    source: bytes,
    path: str,
    statements: dict[str, Statement],
    all_statements: list[Statement],
    seen_ids: set[str],
    *,
    framework: str,
    endpoint: str,
    method: str,
    handler_node: Node | None,
    guards: list[str] | None = None,
    is_regex: bool | None = False,
) -> bool:
    line, column = call.start_point[0] + 1, call.start_point[1]
    statement = statements.get(statement_id(path, line, column))
    if statement is None:
        return False
    handler, handler_line = route_handler(handler_node, source)
    if statement.semanticType == "route":
        if statement.method == method:
            return False
        all_statements.append(
            Statement(
                id=disambiguate(statement_id(path, line, column), seen_ids),
                parentId=statement.parentId,
                nodeType=statement.nodeType,
                semanticType="route",
                text=statement.text,
                method=method,
                endpoint=normalize_endpoint(endpoint),
                handler=handler,
                handlerLine=handler_line,
                framework=framework,
                routeKind="route",
                isRegex=is_regex,
                guards=guards or None,
                startLine=line,
                endLine=statement.endLine,
                path=path,
            )
        )
        return True
    if statement.semanticType is not None:
        return False
    statement.semanticType = "route"
    statement.framework = framework
    statement.method = method
    statement.endpoint = normalize_endpoint(endpoint)
    statement.handler = handler
    statement.handlerLine = handler_line
    statement.routeKind = "route"
    statement.isRegex = is_regex
    statement.guards = guards or None
    return True


def closure_argument(call: Node) -> Node | None:
    for arg in args_of(call):
        node = unwrap(arg)
        if node.type in {"anonymous_function", "arrow_function"}:
            return node
    return None


def closure_body(closure: Node) -> Node:
    return closure.child_by_field_name("body") or closure


def chain_prefix(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    for current in walk(node):
        if current.type not in PHP_CALL_TYPES or method_of(current, source) != "prefix":
            continue
        values = args_of(current)
        prefix = string_value(values[0], source) if values else None
        if prefix is not None:
            return prefix
    return None


def group_filters(node: Node | None, source: bytes) -> list[str]:
    if node is None:
        return []
    node = unwrap(node)
    if node.type != "array_creation_expression":
        return []
    filters: list[str] = []
    for item in node.named_children:
        values = list(item.named_children)
        if len(values) < 2 or node_text(values[0], source).strip("'\"") not in {
            "filter", "filters", "middleware",
        }:
            continue
        filters.extend(strings_in_array(values[-1], source))
        direct = string_value(values[-1], source)
        if direct is not None:
            filters.append(direct)
    return list(dict.fromkeys(filters))
