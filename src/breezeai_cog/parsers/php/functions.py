"""PHP function, method, closure and attribute extraction."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...emit import disambiguate, function_id
from ...schemas import Call, Decorator, Function, Parameter, Statement
from ..callresolve import CallResolver, noop_resolver
from ..treesitter import line_span, node_text
from .statements import extract_statements

_CALL_TYPES = {
    "function_call_expression",
    "member_call_expression",
    "nullsafe_member_call_expression",
    "scoped_call_expression",
}
_NESTED_FUNCTIONS = {"function_definition", "method_declaration", "anonymous_function", "arrow_function"}
_CLASS_TYPES = {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}


def _literal(node: Node, source: bytes) -> str:
    text = node_text(node, source)
    if node.type == "string":
        return text[1:-1] if len(text) >= 2 and text[0] in {"'", '"'} else text
    return text


def extract_attributes(node: Node, source: bytes) -> list[Decorator]:
    attribute_list = node.child_by_field_name("attributes")
    if attribute_list is None:
        return []
    decorators: list[Decorator] = []
    for attr in (child for child in _walk(attribute_list) if child.type == "attribute"):
        name_node = next(
            (child for child in attr.named_children if child.type in {"name", "qualified_name", "namespace_name"}),
            None,
        )
        if name_node is None:
            continue
        name = node_text(name_node, source).lstrip("\\").rsplit("\\", 1)[-1]
        args_node = attr.child_by_field_name("parameters")
        args = []
        if args_node is not None:
            for argument in args_node.named_children:
                value = next(iter(argument.named_children), argument)
                args.append(_literal(value, source))
        decorators.append(Decorator(name=name, args=args))
    return decorators


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _type_text(node: Node | None, source: bytes) -> str:
    if node is None:
        return ""
    return node_text(node, source).lstrip("?")


def extract_params(node: Node | None, source: bytes) -> list[Parameter]:
    if node is None:
        return []
    out: list[Parameter] = []
    for param in node.named_children:
        if param.type not in {"simple_parameter", "variadic_parameter", "property_promotion_parameter"}:
            continue
        name = param.child_by_field_name("name")
        param_type = param.child_by_field_name("type")
        default = param.child_by_field_name("default_value")
        out.append(
            Parameter(
                name=node_text(name, source).lstrip("$") if name is not None else "",
                type=_type_text(param_type, source),
                default=node_text(default, source) if default is not None else None,
                decorators=extract_attributes(param, source),
            )
        )
    return out


def _call_parts(node: Node, source: bytes) -> tuple[str, str | None, str] | None:
    if node.type == "function_call_expression":
        callee_node = node.child_by_field_name("function")
        if callee_node is None:
            return None
        text = node_text(callee_node, source)
        return text, None, text.rsplit("\\", 1)[-1]
    if node.type in {"member_call_expression", "nullsafe_member_call_expression"}:
        receiver = node.child_by_field_name("object")
        method_node = node.child_by_field_name("name")
    else:
        receiver = node.child_by_field_name("scope")
        method_node = node.child_by_field_name("name")
    if method_node is None:
        return None
    method_text = node_text(method_node, source)
    receiver_text = node_text(receiver, source) if receiver is not None else None
    separator = "::" if node.type == "scoped_call_expression" else "->"
    callee = f"{receiver_text}{separator}{method_text}" if receiver_text else method_text
    return callee, receiver_text, method_text


def _calls(
    body: Node | None, source: bytes, resolve: CallResolver
) -> list[Call]:
    if body is None:
        return []
    found: list[Call] = []
    seen: set[tuple[str, str | None]] = set()

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type in _NESTED_FUNCTIONS | _CLASS_TYPES:
                continue
            if child.type in _CALL_TYPES:
                parts = _call_parts(child, source)
                if parts is not None:
                    _, receiver, method = parts
                    key = (method, receiver)
                    if key not in seen:
                        seen.add(key)
                        found.append(Call(name=method, path=resolve(method, receiver)))
            visit(child)

    visit(body)
    return found


def function_node_type(node: Node) -> bool:
    return node.type in _NESTED_FUNCTIONS


def function_parent_id(
    node: Node,
    class_ids: dict[tuple[int, int], str],
    function_ids: dict[tuple[int, int], str],
    fallback: str,
) -> str:
    parent = node.parent
    while parent is not None:
        span = (parent.start_byte, parent.end_byte)
        if span in function_ids:
            return function_ids[span]
        if span in class_ids:
            return class_ids[span]
        parent = parent.parent
    return fallback


def build_function(
    node: Node,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    class_name: str | None,
    seen_ids: set[str],
    capture: bool,
    limit: int,
    resolve: CallResolver = noop_resolver,
    framework: str | None = None,
) -> tuple[Function, list[Statement]]:
    name_node = node.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else "<anonymous>"
    start, end = line_span(node)
    fid = disambiguate(
        function_id(path, name, start, class_name=class_name),
        seen_ids,
    )
    body = node.child_by_field_name("body")
    if body is None and node.type == "arrow_function":
        body = node.child_by_field_name("body")

    fn_type = {
        "function_definition": "function",
        "method_declaration": "constructor" if name == "__construct" else "method",
        "anonymous_function": "function_expression",
        "arrow_function": "arrow_function",
    }.get(node.type, "function")
    visibility = next(
        (node_text(child, source) for child in node.children
         if child.type == "visibility_modifier" or child.type in {"public", "private", "protected"}),
        None,
    )
    is_static = any(child.type == "static_modifier" for child in node.named_children)
    return_type = node.child_by_field_name("return_type")
    fn = Function(
        id=fid,
        parentId=parent_id,
        path=path,
        name=name,
        type=fn_type,
        visibility=visibility,
        isStatic=is_static or None,
        params=extract_params(node.child_by_field_name("parameters"), source),
        decorators=extract_attributes(node, source),
        returnType=_type_text(return_type, source) or None,
        startLine=start,
        endLine=end,
        calls=_calls(body, source, resolve),
    )
    statements = extract_statements(
        body, source, path, parent_id=fid, capture=capture, limit=limit,
        seen_ids=seen_ids, framework=framework,
    )
    return fn, statements
