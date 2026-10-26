"""Slim framework parser with route registration detection."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser
from ..php_route_common import (
    args_of,
    chained_route_metadata,
    chain_prefix,
    closure_argument,
    closure_body,
    enrich_route,
    join_endpoint,
    method_of,
    string_value,
    strings_in_array,
    unwrap,
    walk,
)
from ..treesitter import node_text, parse_source

_VERBS = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "options": "OPTIONS",
    "head": "HEAD",
    "any": "ANY",
}

_ROUTE_RECEIVERS = {"app", "group", "router", "routecollector"}
_ROUTE_METHODS = set(_VERBS) | {"map", "redirect"}
_ROUTE_TYPE_MARKERS = (
    b"RouteCollectorProxy",
    b"RouteCollectorProxyInterface",
    b"RouteCollectorInterface",
    b"Slim\\App",
)


def _closure_parameter_names(closure: Node, source: bytes) -> set[str]:
    parameters = closure.child_by_field_name("parameters")
    if parameters is None:
        return set()
    names: set[str] = set()
    for parameter in parameters.named_children:
        name = parameter.child_by_field_name("name")
        if name is not None:
            names.add(node_text(name, source).lstrip("$").lower())
    return names


def _typed_slim_receiver(receiver_name: str, source: bytes) -> bool:
    if not receiver_name:
        return False
    variable = re.escape(receiver_name.encode())
    return any(
        re.search(re.escape(marker) + rb"\s+\$" + variable + rb"\b", source)
        for marker in _ROUTE_TYPE_MARKERS
    )


def _route_receiver(call: Node, source: bytes, allowed: set[str]) -> bool:
    receiver = call.child_by_field_name("object")
    if receiver is None:
        return False
    receiver_name = node_text(receiver, source).lstrip("$").rsplit("->", 1)[-1].lower()
    return receiver_name in allowed or _typed_slim_receiver(receiver_name, source)


def _route_methods(node: Node, source: bytes) -> tuple[list[str], Node | None, Node | None, str]:
    method = method_of(node, source)
    args = args_of(node)
    if method == "map" and len(args) >= 2:
        methods = [value.upper() for value in strings_in_array(args[0], source)]
        return methods, args[1], args[2] if len(args) > 2 else None, "route"
    if method in _VERBS and args:
        return [_VERBS[method]], args[0], args[1] if len(args) > 1 else None, "route"
    if method == "redirect" and args:
        # A GET binding Slim itself serves: the target is a URL, not a handler of ours.
        return ["GET"], args[0], None, "route"
    return [], None, None, "route"


def _is_class_reference_endpoint(node: Node | None, source: bytes) -> bool:
    value = unwrap(node)
    return value is not None and node_text(value, source).strip().lower().endswith("::class")


def _may_contain_slim_routes(source: bytes) -> bool:
    return any(
        token in source
        for token in (
            b"->get", b"->post", b"->put", b"->patch", b"->delete",
            b"->options", b"->head", b"->any", b"->map", b"->redirect",
            b"->group",
        )
    )


def detect_slim_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    statements = {statement.id: statement for statement in record.statements}
    seen_ids = {statement.id for statement in record.statements}
    found = False

    def visit(
        node: Node,
        prefix: str | None = "",
        inherited_guards: list[str] | None = None,
        allowed_receivers: set[str] | None = None,
    ) -> None:
        nonlocal found
        receivers = allowed_receivers or set(_ROUTE_RECEIVERS)
        guards_in_scope = inherited_guards or []
        if node.type in {"member_call_expression", "scoped_call_expression"}:
            method = method_of(node, source)
            args = args_of(node)
            if method == "group":
                closure = closure_argument(node)
                if closure is not None:
                    base = string_value(args[0], source) if args else None
                    if base is None:
                        base = chain_prefix(node.child_by_field_name("object"), source)
                    first = unwrap(args[0]) if args else None
                    dynamic_base = (
                        (first is not None and first.type not in {"anonymous_function", "arrow_function"}
                         and base is None)
                        or (
                            (object_node := node.child_by_field_name("object")) is not None
                            and chain_prefix(object_node, source) is None
                            and any(method_of(item, source) == "prefix" for item in walk(object_node))
                        )
                    )
                    _, group_guards = chained_route_metadata(
                        node,
                        source,
                        name_methods=set(),
                        guard_methods={"add"},
                    )
                    visit(
                        closure_body(closure),
                        None if dynamic_base else (
                            join_endpoint(prefix or "", base or "") if base else prefix
                        ),
                        list(dict.fromkeys([*guards_in_scope, *group_guards])),
                        receivers | _closure_parameter_names(closure, source),
                    )
                    return
            methods, endpoint_node, handler_node, route_kind = _route_methods(node, source)
            endpoint = string_value(endpoint_node, source)
            if _is_class_reference_endpoint(endpoint_node, source):
                methods = []
            if methods and _route_receiver(node, source, receivers):
                route_endpoint = (
                    join_endpoint(prefix or "", endpoint)
                    if endpoint is not None and prefix is not None else None
                )
                for verb in methods:
                    route_name, guards = chained_route_metadata(
                        node,
                        source,
                        name_methods={"setname"},
                        guard_methods={"add"},
                    )
                    found |= enrich_route(
                        node, source, path, statements, record.statements, seen_ids,
                        framework="slim",
                        endpoint=route_endpoint, method=verb,
                        handler_node=handler_node,
                        name=route_name,
                        guards=list(dict.fromkeys([*guards_in_scope, *guards])),
                        route_kind=route_kind,
                        closure_handler="<closure>",
                    )
        for child in node.named_children:
            visit(child, prefix, guards_in_scope, receivers)

    visit(root)
    return found


class SlimParser(PhpParser):
    name = "php-slim"
    priority = 30
    frameworks = ["slim"]

    def claims(self, path: str, source: bytes) -> bool:
        lower = source.lower()
        return any(marker in lower for marker in (
            b"slim\\", b"namespace slim;", b"routecollectorproxy",
            b"routecollectorinterface", b"slim\\app",
        ))

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        record.framework = "slim"
        if (
            ctx.capture_statements
            and not self.is_fixture_file(ctx.path)
            and _may_contain_slim_routes(ctx.source)
        ):
            detect_slim_routes(root, ctx.source, ctx.path, record)
        return record
