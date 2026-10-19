"""Symfony routing-attribute detection over PHP AST nodes."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Function, Statement
from ..php.index import PhpIndex
from ..php_route_common import join_endpoint, string_value, strings_in_array, walk
from ..treesitter import line_span, node_text

_FRAMEWORK_RESPONSE_TYPES = {
    "response",
    "responseinterface",
    "jsonresponse",
    "redirectresponse",
    "streamedresponse",
    "binaryfileresponse",
}
_REQUEST_PAYLOAD_ATTRIBUTES = {"MapRequestPayload", "MapQueryString"}


def _route_name(attr: Node, source: bytes) -> str | None:
    name = next(
        (child for child in attr.named_children
         if child.type in {"name", "qualified_name", "namespace_name"}),
        None,
    )
    return node_text(name, source).lstrip("\\").rsplit("\\", 1)[-1] if name is not None else None


def _attribute_nodes(node: Node) -> Iterator[Node]:
    attributes = node.child_by_field_name("attributes")
    if attributes is None:
        return
    yield from (child for child in walk(attributes) if child.type == "attribute")


def _args(attr: Node, source: bytes) -> list[tuple[str | None, Node]]:
    params = attr.child_by_field_name("parameters")
    if params is None:
        return []
    result: list[tuple[str | None, Node]] = []
    for argument in params.named_children:
        name = argument.child_by_field_name("name")
        value = next(
            (child for child in argument.named_children if child is not name),
            None,
        )
        if value is not None:
            result.append((node_text(name, source) if name is not None else None, value))
    return result


def _route_values(attr: Node, source: bytes) -> tuple[str, list[str], bool]:
    path: str | None = None
    methods: list[str] = []
    is_regex = False
    for name, value in _args(attr, source):
        if name is None and path is None:
            path = string_value(value, source)
        elif name in {"path", "value"}:
            path = string_value(value, source)
        elif name == "methods":
            methods = [verb.upper() for verb in strings_in_array(value, source)]
        elif name == "requirements":
            is_regex = True
    return path or "", methods or ["ANY"], is_regex


def _dto_type(value: str | None, index: PhpIndex | None) -> str | None:
    if not value or index is None:
        return None
    simple = value.split("<", 1)[0].lstrip("?").rsplit("\\", 1)[-1]
    if simple.lower() in _FRAMEWORK_RESPONSE_TYPES:
        return None
    matches = [fqn for fqn, file_path in index.fqcn.items()
               if file_path is not None and fqn.rsplit("\\", 1)[-1] == simple]
    return simple if len(matches) == 1 else None


def _request_dto(fn: Function, index: PhpIndex | None) -> str | None:
    for param in fn.params:
        if any(dec.name in _REQUEST_PAYLOAD_ATTRIBUTES for dec in param.decorators):
            return _dto_type(param.type, index)
    return None


def _route_decorators(node: Node, source: bytes) -> list[Node]:
    return [attr for attr in _attribute_nodes(node) if _route_name(attr, source) == "Route"]


def detect_symfony_routes(
    root: Node, source: bytes, path: str, record: FileRecord, index: PhpIndex | None
) -> int:
    classes: list[Node] = [
        node for node in walk(root) if node.type in {
            "class_declaration", "interface_declaration", "trait_declaration", "enum_declaration",
        }
    ]
    class_routes: dict[tuple[int, int], list[tuple[str, bool]]] = {}
    for cls_node in classes:
        attrs = _route_decorators(cls_node, source)
        if attrs:
            class_routes[(cls_node.start_byte, cls_node.end_byte)] = [
                _route_values(attr, source)[::2] for attr in attrs
            ]

    seen = {statement.id for statement in record.statements}
    emitted = 0
    fn_nodes = [
        node for node in walk(root)
        if node.type in {"method_declaration", "function_definition", "anonymous_function", "arrow_function"}
    ]
    for fn, fn_node in zip(record.functions, fn_nodes):
        prefixes: list[tuple[str, bool]] = [("", False)]
        parent = fn_node.parent
        while parent is not None:
            key = (parent.start_byte, parent.end_byte)
            if key in class_routes:
                prefixes = class_routes[key]
                break
            parent = parent.parent
        for attr in _route_decorators(fn_node, source):
            subpath, methods, is_regex = _route_values(attr, source)
            for prefix, parent_regex in prefixes:
                for method in methods:
                    source_node = (
                        attr.parent
                        if attr.parent is not None and attr.parent.type == "attribute_group"
                        else attr
                    )
                    start, end = line_span(source_node)
                    statement = Statement(
                        id=disambiguate(
                            statement_id(path, start, source_node.start_point[1]), seen
                        ),
                        parentId=fn.id,
                        nodeType="synthetic",
                        semanticType="route",
                        text=node_text(source_node, source),
                        method=method,
                        endpoint=join_endpoint(prefix, subpath),
                        framework="symfony",
                        handler=fn.name,
                        handlerLine=fn.startLine,
                        routeKind="route",
                        isRegex=is_regex or parent_regex,
                        requestDTO=_request_dto(fn, index),
                        responseDTO=_dto_type(fn.returnType, index),
                        startLine=start,
                        endLine=end,
                        path=path,
                    )
                    record.statements.append(statement)
                    emitted += 1
    return emitted


def strip_route_decorators(record: FileRecord) -> None:
    for cls in record.classes:
        cls.decorators = [dec for dec in cls.decorators if dec.name != "Route"]
    for fn in record.functions:
        fn.decorators = [dec for dec in fn.decorators if dec.name != "Route"]
