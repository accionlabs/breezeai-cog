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


def unwrap(node: Node | None) -> Node | None:
    while node is not None:
        if node.type == "argument" and len(node.named_children) == 1:
            node = node.named_children[0]
        elif node.type == "parenthesized_expression" and len(node.named_children) == 1:
            node = node.named_children[0]
        else:
            break
    return node


def args_of(call: Node) -> list[Node]:
    args = call.child_by_field_name("arguments")
    return list(args.named_children) if args is not None else []


def method_of(call: Node, source: bytes) -> str | None:
    name = call.child_by_field_name("name")
    return node_text(name, source).lower() if name is not None else None


def string_value(node: Node | None, source: bytes) -> str | None:
    """The value of a plain PHP string literal: single-quoted (``string``) or double-quoted
    (``encapsed_string``) with no interpolation or escape sequence, else None."""
    if node is None:
        return None
    node = unwrap(node)
    if node is None or node.type not in {"string", "encapsed_string"}:
        return None
    if any(child.type != "string_content" for child in node.named_children):
        return None
    return "".join(node_text(child, source) for child in node.named_children)


def strings_in_array(node: Node | None, source: bytes) -> list[str]:
    if node is None:
        return []
    node = unwrap(node)
    if node is None:
        return []
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
    if node is None:
        return None, None

    if node.type == "binary_expression":
        left = unwrap(node.child_by_field_name("left"))
        right = unwrap(node.child_by_field_name("right"))
        cls: str | None = None
        if left is not None:
            if left.type == "class_constant_access_expression":
                cls = node_text(left, source).rsplit("::", 1)[0].lstrip("\\")
            else:
                s = string_value(left, source)
                cls = s.lstrip("\\") if s else None
        method = string_value(right, source) if right is not None else None
        if cls and method:
            clean_method = method.lstrip(":").lstrip("@").split("/", 1)[0]
            return f"{cls}@{clean_method}", None

    literal = string_value(node, source)
    if literal is not None:
        literal = literal.lstrip("\\")
        if "::" in literal:
            cls, method = literal.rsplit("::", 1)
            return f"{cls}@{method.split('/', 1)[0]}", None
        if "@" in literal:
            cls, method = literal.rsplit("@", 1)
            return f"{cls}@{method.split('/', 1)[0]}", None
        if ":" in literal:
            cls, method = literal.rsplit(":", 1)
            return f"{cls}@{method.split('/', 1)[0]}", None
        return literal, None

    if node.type == "class_constant_access_expression":
        cls = node_text(node, source).rsplit("::", 1)[0].lstrip("\\")
        return cls, None

    if node.type != "array_creation_expression":
        return None, None
    elements = list(node.named_children)
    if len(elements) < 2:
        return None, None
    values = [unwrap(next(iter(e.named_children), None)) for e in elements]
    method_name = string_value(values[1], source)
    if method_name is None:
        return None, None
    clean_method = method_name.lstrip(":").lstrip("@").split("/", 1)[0]
    target = values[0]
    class_name: str | None = None
    if target is not None:
        if target.type == "class_constant_access_expression":
            class_name = node_text(target, source).rsplit("::", 1)[0].lstrip("\\")
        else:
            s = string_value(target, source)
            class_name = s.lstrip("\\") if s else None
    return (f"{class_name}@{clean_method}" if class_name else clean_method), None


def enrich_route(
    call: Node,
    source: bytes,
    path: str,
    statements: dict[str, Statement],
    all_statements: list[Statement],
    seen_ids: set[str],
    *,
    framework: str,
    endpoint: str | None,
    method: str | None,
    handler_node: Node | None,
    name: str | None = None,
    guards: list[str] | None = None,
    is_regex: bool | None = False,
    route_kind: str = "route",
    handler_name: str | None = None,
) -> bool:
    line, column = call.start_point[0] + 1, call.start_point[1]
    statement = statements.get(statement_id(path, line, column))
    if statement is None:
        return False
    handler, handler_line = route_handler(handler_node, source)
    if handler_name is not None:
        handler, handler_line = handler_name, None
    if handler is None and handler_node is not None:
        closure = unwrap(handler_node)
        if closure is not None and closure.type in {"anonymous_function", "arrow_function"}:
            # An inline handler has no name: the contract records the line it starts on.
            handler_line = closure.start_point[0] + 1
    if statement.semanticType == "route":
        normalized_endpoint = normalize_endpoint(endpoint) if endpoint is not None else None
        if (statement.method, statement.endpoint, statement.handler) == (method, normalized_endpoint, handler):
            changed = False
            if name and statement.name != name:
                statement.name = name
                changed = True
            merged_guards = list(dict.fromkeys([*(statement.guards or []), *(guards or [])]))
            if merged_guards and statement.guards != merged_guards:
                statement.guards = merged_guards
                changed = True
            return changed
        all_statements.append(
            Statement(
                id=disambiguate(statement_id(path, line, column), seen_ids),
                parentId=statement.parentId,
                nodeType=statement.nodeType,
                semanticType="route",
                text=statement.text,
                method=method,
                endpoint=normalize_endpoint(endpoint) if endpoint is not None else None,
                handler=handler,
                handlerLine=handler_line,
                name=name or statement.name,
                framework=framework,
                routeKind=route_kind,
                isRegex=is_regex,
                guards=list(dict.fromkeys([*(statement.guards or []), *(guards or [])])) or None,
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
    statement.endpoint = normalize_endpoint(endpoint) if endpoint is not None else None
    statement.handler = handler
    statement.handlerLine = handler_line
    statement.name = name
    statement.routeKind = route_kind
    statement.isRegex = is_regex
    statement.guards = list(dict.fromkeys(guards or [])) or None
    return True


def closure_argument(call: Node) -> Node | None:
    for arg in args_of(call):
        node = unwrap(arg)
        if node is not None and node.type in {"anonymous_function", "arrow_function"}:
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


def chained_route_metadata(
    call: Node,
    source: bytes,
    *,
    name_methods: set[str],
    guard_methods: set[str],
) -> tuple[str | None, list[str]]:
    """Collect literal name and middleware/guard values on a route's fluent chain."""
    route_name: str | None = None
    guards: list[str] = []
    current = call

    while current.parent is not None:
        parent = current.parent
        if parent.type == "parenthesized_expression" and len(parent.named_children) == 1:
            current = parent
            continue
        if parent.type not in PHP_CALL_TYPES:
            break
        receiver = unwrap(parent.child_by_field_name("object"))
        if receiver is None or (
            receiver.start_byte != current.start_byte
            or receiver.end_byte != current.end_byte
        ):
            break

        method = method_of(parent, source)
        args = args_of(parent)
        if args and method in name_methods:
            route_name = string_value(args[0], source) or route_name
        elif args and method in guard_methods:
            guards.extend(route_guard_values(args[0], source))
        current = parent

    return route_name, list(dict.fromkeys(guards))


def receiver_chain_metadata(
    call: Node,
    source: bytes,
    *,
    name_methods: set[str],
    guard_methods: set[str],
) -> tuple[str | None, list[str]]:
    """Collect literal metadata from calls that configure a fluent receiver."""
    route_names: list[str] = []
    guards: list[str] = []
    current = unwrap(call.child_by_field_name("object"))
    while current is not None and current.type in PHP_CALL_TYPES:
        method = method_of(current, source)
        args = args_of(current)
        if args and method in name_methods:
            name = string_value(args[0], source)
            if name is not None:
                route_names.insert(0, name)
        elif args and method in guard_methods:
            guards.extend(route_guard_values(args[0], source))
        current = unwrap(current.child_by_field_name("object"))
    return "".join(route_names) or None, list(dict.fromkeys(guards))


def route_guard_values(node: Node | None, source: bytes) -> list[str]:
    """Return literal middleware identifiers without guessing dynamic expressions."""
    if node is None:
        return []
    node = unwrap(node)
    if node is None:
        return []
    if node.type == "array_creation_expression":
        return list(dict.fromkeys(
            value
            for item in node.named_children
            for value in route_guard_values(item, source)
        ))
    if node.type == "array_element_initializer":
        child = next(iter(node.named_children), None)
        return route_guard_values(child, source)
    value = string_value(node, source)
    if value is not None:
        return [value]
    if node.type == "class_constant_access_expression":
        text = node_text(node, source)
        class_name, separator, constant = text.rpartition("::")
        if separator and constant.lower() == "class":
            return [class_name.lstrip("\\")]
    if node.type == "object_creation_expression":
        class_node = next(iter(node.named_children), None)
        if class_node is not None and class_node.type in {"name", "qualified_name"}:
            return [node_text(class_node, source).lstrip("\\")]
    return []


def route_options(
    node: Node | None, source: bytes
) -> tuple[str | None, list[str]]:
    node = unwrap(node)
    if node is None or node.type != "array_creation_expression":
        return None, []
    route_name: str | None = None
    guards: list[str] = []
    for item in node.named_children:
        values = list(item.named_children)
        if len(values) < 2:
            continue
        key = node_text(values[0], source).strip("'\"")
        if key in {"as", "name"}:
            route_name = string_value(values[-1], source) or route_name
        elif key in {"filter", "filters", "middleware"}:
            guards.extend(route_guard_values(values[-1], source))
    return route_name, list(dict.fromkeys(guards))


def group_filters(node: Node | None, source: bytes) -> list[str]:
    if node is None:
        return []
    node = unwrap(node)
    if node is None or node.type != "array_creation_expression":
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


_CLASS_SCOPES = {"class_declaration", "trait_declaration", "enum_declaration"}
_SELF = {"$this", "self::class", "static::class", "__CLASS__"}


def _enclosing_class(node: Node, source: bytes) -> str | None:
    parent = node.parent
    while parent is not None:
        if parent.type in _CLASS_SCOPES:
            name = parent.child_by_field_name("name")
            return node_text(name, source) if name is not None else None
        parent = parent.parent
    return None


def callback_handler(node: Node, source: bytes) -> tuple[str | None, int | None]:
    """(handler as written, line of an inline closure). The handler of a named callback is
    located after parsing; a callback on an object whose class the syntax doesn't show
    (``[$service, 'run']``) names no handler — a bare ``run`` would be read as a global
    function."""
    node = unwrap(node) or node
    if node.type in {"anonymous_function", "arrow_function"}:
        return None, node.start_point[0] + 1
    value = string_value(node, source)
    if value is not None:
        cls, sep, name = value.lstrip("\\").rpartition("::")
        return (f"{cls}@{name}" if sep else value.lstrip("\\")), None
    if node.type != "array_creation_expression" or len(node.named_children) != 2:
        return None, None
    target, member = (unwrap(next(iter(e.named_children), None)) for e in node.named_children)
    method = string_value(member, source)
    if target is None or method is None:
        return None, None
    written = node_text(target, source)
    owner: str | None
    if written in _SELF:
        owner = _enclosing_class(node, source)
    elif target.type == "class_constant_access_expression" and written.lower().endswith("::class"):
        owner = written.rsplit("::", 1)[0].lstrip("\\")
    else:
        owner = (string_value(target, source) or "").lstrip("\\")
    return (f"{owner}@{method}" if owner else None), None
