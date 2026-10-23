"""Symfony route, configurator and event detection over PHP AST nodes."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Function, SemanticType, Statement
from ..php.index import PhpIndex
from ..php_route_common import join_endpoint, string_value, strings_in_array, walk
from ..treesitter import line_span, node_text

_FRAMEWORK_RESPONSE_TYPES = {
    "response", "responseinterface", "jsonresponse", "redirectresponse",
    "streamedresponse", "binaryfileresponse",
}
_REQUEST_PAYLOAD_ATTRIBUTES = {"MapRequestPayload", "MapQueryString"}
_CALL_TYPES = {"member_call_expression", "nullsafe_member_call_expression", "scoped_call_expression"}


@dataclass(frozen=True)
class _RouteSpec:
    paths: dict[str | None, str] | None
    methods: tuple[str, ...]
    is_regex: bool
    name: str | None
    guards: tuple[str, ...] = ()


def _route_name(attr: Node, source: bytes) -> str | None:
    name = next((child for child in attr.named_children
                 if child.type in {"name", "qualified_name", "namespace_name"}), None)
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
        value = next((child for child in argument.named_children
                      if name is None or (child.start_byte, child.end_byte) != (name.start_byte, name.end_byte)), None)
        if value is not None:
            result.append((node_text(name, source) if name is not None else None, value))
    return result


def _string_map(node: Node | None, source: bytes) -> dict[str | None, str] | None:
    if node is None or node.type != "array_creation_expression":
        return None
    values: dict[str | None, str] = {}
    for element in node.named_children:
        key = element.child_by_field_name("key")
        value = element.child_by_field_name("value")
        if key is None or value is None:
            children = list(element.named_children)
            if len(children) != 2:
                return None
            key, value = children
        key_text = string_value(key, source)
        value_text = string_value(value, source)
        if key_text is None or value_text is None:
            return None
        values[key_text] = value_text
    return values or None


def _path_options(value: Node, source: bytes) -> dict[str | None, str] | None:
    scalar = string_value(value, source)
    if scalar is not None:
        return {None: scalar}
    localized = _string_map(value, source)
    return localized if localized else None


def _route_values(attr: Node, source: bytes) -> _RouteSpec:
    paths: dict[str | None, str] | None = {None: ""}
    path_seen = False
    methods: list[str] = []
    is_regex = False
    route_name: str | None = None
    for argument_name, value in _args(attr, source):
        if argument_name is None and not path_seen:
            paths = _path_options(value, source)
            path_seen = True
        elif argument_name in {"path", "value"}:
            paths = _path_options(value, source)
            path_seen = True
        elif argument_name == "methods":
            methods = [verb.upper() for verb in strings_in_array(value, source)]
        elif argument_name == "requirements":
            is_regex = True
        elif argument_name == "name":
            route_name = string_value(value, source)
    return _RouteSpec(paths, tuple(methods or ["ANY"]), is_regex, route_name)


def _security_guards(node: Node, source: bytes) -> list[str]:
    guards: list[str] = []
    for attr in _attribute_nodes(node):
        name = _route_name(attr, source)
        if name not in {"IsGranted", "Security"}:
            continue
        args = _args(attr, source)
        value = string_value(args[0][1], source) if args else None
        guards.append(f"{name}:{value}" if value is not None else name)
    return list(dict.fromkeys(guards))


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


def _route_attribute_names(root: Node, source: bytes) -> set[str]:
    text = source.decode("utf-8", "replace")
    names = {"Route"}
    for match in re.finditer(r"\buse\s+[^;]*\\Route\s+as\s+([A-Za-z_]\w*)\s*;", text):
        names.add(match.group(1))
    if "Symfony\\Component\\Routing" in text:
        names.update(
            name for attr in walk(root) if attr.type == "attribute"
            for name in [_route_name(attr, source)]
            if name and name.lower().endswith("route")
        )
    return names


def _route_decorators(node: Node, source: bytes, route_names: set[str]) -> list[Node]:
    return [attr for attr in _attribute_nodes(node) if _route_name(attr, source) in route_names]


def _function_for_node(fn_node: Node, record: FileRecord) -> Function | None:
    line = fn_node.start_point[0] + 1
    candidates = [fn for fn in record.functions if fn.startLine <= line <= fn.endLine]
    return max(candidates, key=lambda fn: (fn.startLine, -fn.endLine)) if candidates else None


def _path_combinations(prefix: dict[str | None, str] | None,
                       suffix: dict[str | None, str] | None) -> list[tuple[str | None, str | None]]:
    if prefix is None or suffix is None:
        return [(None, None)]
    locales = set(prefix) | set(suffix)
    if locales <= {None}:
        return [(None, join_endpoint(prefix.get(None, ""), suffix.get(None, "")))]
    named_locales = sorted(locale for locale in locales if locale is not None)
    return [(locale, join_endpoint(prefix.get(locale, prefix.get(None, "")),
                                   suffix.get(locale, suffix.get(None, ""))))
            for locale in named_locales]


def _route_statement(*, path: str, source_node: Node, source: bytes, parent_id: str, seen: set[str],
                     method: str, endpoint: str | None, name: str | None, handler: Function | None,
                     is_regex: bool, guards: list[str], request_dto: str | None,
                     response_dto: str | None) -> Statement:
    start, end = line_span(source_node)
    return Statement(
        id=disambiguate(statement_id(path, start, source_node.start_point[1]), seen),
        parentId=parent_id, nodeType="synthetic", semanticType="route",
        text=node_text(source_node, source), method=method, endpoint=endpoint,
        framework="symfony", name=name, handler=handler.name if handler else None,
        handlerLine=handler.startLine if handler else None, routeKind="route", isRegex=is_regex,
        guards=guards or None, authRequired=bool(guards) or None,
        requestDTO=request_dto, responseDTO=response_dto, startLine=start, endLine=end, path=path,
    )


def detect_symfony_routes(root: Node, source: bytes, path: str, record: FileRecord,
                          index: PhpIndex | None) -> set[str]:
    route_names = _route_attribute_names(root, source)
    classes = [node for node in walk(root) if node.type in {
        "class_declaration", "interface_declaration", "trait_declaration", "enum_declaration",
    }]
    class_routes: dict[tuple[int, int], list[_RouteSpec]] = {}
    for cls_node in classes:
        routes = []
        for attr in _route_decorators(cls_node, source, route_names):
            spec = _route_values(attr, source)
            routes.append(_RouteSpec(spec.paths, spec.methods, spec.is_regex, spec.name,
                                     tuple(_security_guards(cls_node, source))))
        if routes:
            class_routes[(cls_node.start_byte, cls_node.end_byte)] = routes

    seen = {statement.id for statement in record.statements}
    emitted = 0
    fn_nodes = [node for node in walk(root) if node.type in {
        "method_declaration", "function_definition", "anonymous_function", "arrow_function",
    }]
    for fn_node in fn_nodes:
        fn = _function_for_node(fn_node, record)
        if fn is None:
            continue
        prefixes: list[_RouteSpec] = [_RouteSpec({None: ""}, ("ANY",), False, None)]
        has_class_route = False
        parent = fn_node.parent
        while parent is not None:
            key = (parent.start_byte, parent.end_byte)
            if key in class_routes:
                prefixes = class_routes[key]
                has_class_route = True
                break
            parent = parent.parent

        attrs = _route_decorators(fn_node, source, route_names)
        if attrs:
            method_specs = [_route_values(attr, source) for attr in attrs]
        elif fn.name == "__invoke" and has_class_route:
            method_specs = [_RouteSpec({None: ""}, prefix.methods, False, None, prefix.guards)
                            for prefix in prefixes]
        else:
            continue

        attrs_for_specs: list[Node | None] = list(attrs)
        if not attrs_for_specs:
            attrs_for_specs = [None] * len(method_specs)
        for route_attr, method_spec in zip(attrs_for_specs, method_specs):
            source_node = fn_node
            if route_attr is not None:
                route_parent = route_attr.parent
                source_node = (route_parent if route_parent is not None
                               and route_parent.type == "attribute_group" else route_attr)
            for prefix in prefixes:
                methods = (prefix.methods if attrs and method_spec.methods == ("ANY",)
                           and prefix.methods != ("ANY",) else method_spec.methods)
                name = f"{prefix.name or ''}{method_spec.name or ''}" or None
                guards = list(dict.fromkeys([*prefix.guards, *_security_guards(fn_node, source)]))
                for _, endpoint in _path_combinations(prefix.paths, method_spec.paths):
                    for method in methods:
                        statement = _route_statement(
                            path=path, source_node=source_node, source=source, parent_id=fn.id, seen=seen,
                            method=method, endpoint=endpoint, name=name, handler=fn,
                            is_regex=prefix.is_regex or method_spec.is_regex, guards=guards,
                            request_dto=_request_dto(fn, index), response_dto=_dto_type(fn.returnType, index),
                        )
                        seen.add(statement.id)
                        record.statements.append(statement)
                        emitted += 1
    return route_names if emitted else set()


def _call_method(node: Node, source: bytes) -> str | None:
    name = node.child_by_field_name("name")
    return node_text(name, source) if name is not None else None


def _call_args(node: Node) -> list[Node]:
    args = node.child_by_field_name("arguments")
    return list(args.named_children) if args is not None else []


def _call_chain_value(expression: Node, source: bytes, method: str) -> str | None:
    for node in walk(expression):
        if node.type in _CALL_TYPES and _call_method(node, source) == method:
            args = _call_args(node)
            if args:
                value = string_value(args[0], source)
                if value is not None:
                    return value
    return None


def _call_chain_array(expression: Node, source: bytes, method: str) -> list[str]:
    for node in walk(expression):
        if node.type in _CALL_TYPES and _call_method(node, source) == method:
            args = _call_args(node)
            if args:
                return strings_in_array(args[0], source)
    return []


def _routing_builder_receiver(receiver: str) -> bool:
    low = receiver.lower().replace("$", "")
    terminal = re.split(r"->|\\", low)[-1]
    return terminal in {"routes", "collection", "routecollection"} or "route" in terminal


def detect_symfony_route_builders(root: Node, source: bytes, path: str, record: FileRecord) -> int:
    source_lower = source.decode("utf-8", "replace").lower()
    if "routingconfigurator" not in source_lower and "routecollection" not in source_lower:
        return 0
    seen = {statement.id for statement in record.statements}
    emitted = 0
    for call in walk(root):
        if call.type not in _CALL_TYPES or _call_method(call, source) != "add":
            continue
        receiver = call.child_by_field_name("object")
        if receiver is None or not _routing_builder_receiver(node_text(receiver, source)):
            continue
        args = _call_args(call)
        name = string_value(args[0], source) if args else None
        endpoint = string_value(args[1], source) if len(args) > 1 else None
        expression = call
        while expression.parent is not None and expression.parent.type != "expression_statement":
            expression = expression.parent
        text = node_text(expression, source)
        if name is None and endpoint is None:
            continue
        line = call.start_point[0] + 1
        parent_id = next((fn.id for fn in record.functions if fn.startLine <= line <= fn.endLine), record.id)
        methods = [verb.upper() for verb in _call_chain_array(expression, source, "methods")] or ["ANY"]
        controller = _call_chain_value(expression, source, "controller")
        requirements = any(node.type in _CALL_TYPES and _call_method(node, source) == "requirements"
                           for node in walk(expression))
        for method in methods:
            statement = Statement(
                id=disambiguate(statement_id(path, line, call.start_point[1]), seen), parentId=parent_id,
                nodeType="synthetic", semanticType="route", text=text, method=method,
                endpoint=join_endpoint("", endpoint) if endpoint is not None else None,
                framework="symfony", name=name, handler=controller, handlerLine=line,
                routeKind="route", isRegex=requirements, startLine=line,
                endLine=expression.end_point[0] + 1, path=path,
            )
            seen.add(statement.id)
            record.statements.append(statement)
            emitted += 1
    return emitted


def _event_statement(call: Node, source: bytes, path: str, record: FileRecord, seen: set[str],
                     semantic: SemanticType, endpoint: str | None) -> Statement:
    line, column = call.start_point[0] + 1, call.start_point[1]
    parent_id = next((fn.id for fn in record.functions if fn.startLine <= line <= fn.endLine), record.id)
    statement = Statement(
        id=disambiguate(statement_id(path, line, column), seen), parentId=parent_id,
        nodeType=call.type, semanticType=semantic, text=node_text(call, source),
        framework="symfony", method=_call_method(call, source), endpoint=endpoint,
        startLine=line, endLine=call.end_point[0] + 1, path=path,
    )
    seen.add(statement.id)
    return statement


def detect_symfony_events(root: Node, source: bytes, path: str, record: FileRecord) -> int:
    source_lower = source.decode("utf-8", "replace").lower()
    has_dispatcher = "eventdispatcher" in source_lower or "event_dispatcher" in source_lower
    has_messenger = "messagebus" in source_lower or "symfony\\component\\messenger" in source_lower
    if not has_dispatcher and not has_messenger:
        return 0
    seen = {statement.id for statement in record.statements}
    emitted = 0
    for call in walk(root):
        if call.type not in _CALL_TYPES:
            continue
        method = (_call_method(call, source) or "").lower()
        if method not in {"dispatch", "addlistener", "addsubscriber"}:
            continue
        receiver_node = call.child_by_field_name("object") or call.child_by_field_name("scope")
        receiver = node_text(receiver_node, source).lower() if receiver_node is not None else ""
        terminal = re.split(r"->|::", receiver)[-1].replace("$", "")
        if terminal in {"this", "self", "static"}:
            continue
        is_dispatcher = has_dispatcher and ("dispatcher" in receiver or terminal in {
            "dispatcher", "eventdispatcher", "eventdispatcherinterface",
        })
        is_messenger = has_messenger and ("messagebus" in receiver or terminal in {
            "bus", "messagebus", "eventbus",
        })
        if method == "dispatch":
            semantic: SemanticType
            if is_messenger:
                semantic, endpoint = "eventbus_send", None
            elif is_dispatcher:
                args = _call_args(call)
                semantic = "eventbus_publish"
                endpoint = string_value(args[1], source) if len(args) > 1 else None
            else:
                continue
        elif is_dispatcher:
            args = _call_args(call)
            semantic = "eventbus_consumer"
            endpoint = string_value(args[0], source) if args else None
        else:
            continue
        record.statements.append(_event_statement(call, source, path, record, seen, semantic, endpoint))
        emitted += 1
    return emitted


def strip_route_decorators(record: FileRecord, route_names: set[str] | None = None) -> None:
    names = route_names or {"Route"}
    for cls in record.classes:
        cls.decorators = [dec for dec in cls.decorators if dec.name not in names]
    for fn in record.functions:
        fn.decorators = [dec for dec in fn.decorators if dec.name not in names]
