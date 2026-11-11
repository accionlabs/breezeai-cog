"""WordPress hook registrations as additive event consumers."""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..php_route_common import (
    callback_handler,
    join_endpoint,
    string_value,
    unwrap,
    walk,
)
from ..treesitter import node_text


def detect_wordpress_hooks(dc: DetectContext) -> Detection | None:
    source = dc.source
    if b"add_action" not in source and b"add_filter" not in source:
        return None
    found: list[Emit] = []
    for node in walk(dc.root):
        if node.type != "function_call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or node_text(function, source).rsplit("\\", 1)[-1] not in {
            "add_action", "add_filter",
        }:
            continue
        args = node.child_by_field_name("arguments")
        values = list(args.named_children) if args is not None else []
        if len(values) < 2:
            continue
        # An unresolved hook name is still a binding: its endpoint stays empty (contract §8).
        hook = string_value(values[0], source)
        handler, handler_line = callback_handler(values[1], source)
        found.append(Emit(
            node,
            {
                "semanticType": "eventbus_consumer",
                "method": "CONSUMER",
                "endpoint": hook,
                "handler": handler,
                "handlerLine": handler_line,
                "framework": "wordpress",
            },
            enrich="unclassified",
        ))
    return Detection(found, label="wordpress") if found else None


_REST_METHODS = {
    "readable": ("GET",),
    "creatable": ("POST",),
    "editable": ("POST", "PUT", "PATCH"),
    "deletable": ("DELETE",),
    "allmethods": ("GET", "POST", "PUT", "PATCH", "DELETE"),
}
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}


def _array_pairs(node: Node | None, source: bytes) -> dict[str, Node]:
    node = unwrap(node)
    if node is None or node.type != "array_creation_expression":
        return {}
    pairs: dict[str, Node] = {}
    for element in node.named_children:
        children = list(element.named_children)
        if len(children) < 2:
            continue
        key = string_value(children[0], source)
        if key is not None:
            pairs[key.lower()] = children[-1]
    return pairs


def _route_options(node: Node, source: bytes) -> list[dict[str, Node]]:
    pairs = _array_pairs(node, source)
    if "methods" in pairs or "callback" in pairs:
        return [pairs]
    array = unwrap(node)
    if array is None or array.type != "array_creation_expression":
        return [{}]
    options = [
        nested
        for element in array.named_children
        if (nested := _array_pairs(element, source))
        and ("methods" in nested or "callback" in nested)
    ]
    return options or [{}]


def _method_values(node: Node, source: bytes) -> list[str | None]:
    values = [value.upper() for value in _string_values(node, source)]
    if values:
        return [value if value in _HTTP_METHODS else None for value in values]
    value_node = unwrap(node)
    if value_node is None or value_node.type != "class_constant_access_expression":
        return [None]
    children = list(value_node.named_children)
    if len(children) < 2 or node_text(children[-2], source).lstrip("\\").lower() != "wp_rest_server":
        return [None]
    return list(_REST_METHODS.get(node_text(children[-1], source).lower(), (None,)))


def _string_values(node: Node, source: bytes) -> list[str]:
    value_node = unwrap(node)
    if value_node is None:
        return []
    if value_node.type == "array_creation_expression":
        values: list[str] = []
        for element in value_node.named_children:
            value = string_value(next(iter(element.named_children), None), source)
            if value is not None:
                values.append(value)
        return values
    value = string_value(value_node, source)
    return [value] if value is not None else []


def detect_wordpress_routes(dc: DetectContext) -> Detection | None:
    source = dc.source
    if b"register_rest_route" not in source:
        return None
    found: list[Emit] = []
    for node in walk(dc.root):
        if node.type != "function_call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or node_text(function, source).rsplit("\\", 1)[-1] != "register_rest_route":
            continue
        args = node.child_by_field_name("arguments")
        values = list(args.named_children) if args is not None else []
        if len(values) < 2:
            continue
        namespace, path = string_value(values[0], source), string_value(values[1], source)
        endpoint = join_endpoint(namespace, path) if namespace is not None and path is not None else None
        options = _route_options(values[2], source) if len(values) > 2 else [{}]
        for option in options:
            callback_node = option.get("callback")
            callback, callback_line = (
                callback_handler(callback_node, source)
                if callback_node is not None
                else (None, None)
            )
            methods = _method_values(option["methods"], source) if "methods" in option else [None]
            for method in methods:
                found.append(Emit(
                    node,
                    {
                        "semanticType": "route",
                        "method": method,
                        "endpoint": endpoint,
                        "handler": callback,
                        "handlerLine": callback_line,
                        "routeKind": "route",
                        "isRegex": bool(endpoint and ("(?P<" in endpoint or "(?<" in endpoint)),
                        "framework": "wordpress",
                    },
                    enrich="unclassified",
                ))
    return Detection(found, label="wordpress") if found else None


register_detector(
    Detector(
        name="wordpress-hooks",
        gate=Gate("php", order=10, skip_fixtures=True),
        detect=detect_wordpress_hooks,
        frameworks=("wordpress",),
    )
)

register_detector(
    Detector(
        name="wordpress-rest-routes",
        gate=Gate("php", order=11, guard=b"register_rest_route", skip_fixtures=True),
        detect=detect_wordpress_routes,
        frameworks=("wordpress",),
    )
)
