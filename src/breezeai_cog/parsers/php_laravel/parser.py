"""Laravel framework parser with call-based route detection."""

from __future__ import annotations

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
    receiver_chain_metadata,
    route_options,
    string_value,
    strings_in_array,
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


def _route_scope(call: Node, source: bytes) -> bool:
    scope = call.child_by_field_name("scope")
    return scope is not None and node_text(scope, source).rsplit("\\", 1)[-1] == "Route"


def _direct_prefix(call: Node, source: bytes) -> str | None:
    prefix = chain_prefix(call.child_by_field_name("object"), source)
    if prefix is not None:
        return prefix
    args = args_of(call)
    if args:
        direct = string_value(args[0], source)
        if direct is not None:
            return direct
        options = args[0].named_children[0] if args[0].named_children else None
        if options is not None and options.type == "array_creation_expression":
            for pair in options.named_children:
                parts = list(pair.named_children)
                if len(parts) > 1 and node_text(parts[0], source).strip("'\"") == "prefix":
                    return string_value(parts[-1], source)
    return None


def detect_laravel_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    statements = {statement.id: statement for statement in record.statements}
    seen_ids = {statement.id for statement in record.statements}
    found = False

    def visit(
        node: Node,
        prefix: str = "",
        name_prefix: str = "",
        inherited_guards: list[str] | None = None,
    ) -> None:
        nonlocal found
        if node.type in {"member_call_expression", "scoped_call_expression"}:
            name = method_of(node, source)
            if name == "group" and (node.type != "scoped_call_expression" or _route_scope(node, source)):
                closure = closure_argument(node)
                if closure is not None:
                    next_prefix = _direct_prefix(node, source)
                    group_name, group_guards = receiver_chain_metadata(
                        node,
                        source,
                        name_methods={"name"},
                        guard_methods={"middleware"},
                    )
                    args = args_of(node)
                    option_name, option_guards = route_options(args[0], source) if args else (None, [])
                    visit(
                        closure_body(closure),
                        join_endpoint(prefix, next_prefix or "") if next_prefix else prefix,
                        f"{name_prefix}{option_name or ''}{group_name or ''}",
                        list(dict.fromkeys([
                            *(inherited_guards or []), *option_guards, *group_guards,
                        ])),
                    )
                    return
            if node.type == "scoped_call_expression" and _route_scope(node, source):
                args = args_of(node)
                if name == "match" and len(args) >= 2:
                    methods = [m.upper() for m in strings_in_array(args[0], source)]
                    endpoint_node, handler_node = args[1], args[2] if len(args) > 2 else None
                elif name in _VERBS and args:
                    methods = [_VERBS[name]]
                    endpoint_node, handler_node = args[0], args[1] if len(args) > 1 else None
                else:
                    methods = []
                    endpoint_node, handler_node = None, None
                endpoint = string_value(endpoint_node, source)
                if endpoint is not None:
                    for method in methods:
                        route_name, guards = chained_route_metadata(
                            node,
                            source,
                            name_methods={"name"},
                            guard_methods={"middleware"},
                        )
                        if route_name:
                            route_name = f"{name_prefix}{route_name}"
                        found |= enrich_route(
                            node, source, path, statements, record.statements, seen_ids,
                            framework="laravel",
                            endpoint=join_endpoint(prefix, endpoint), method=method,
                            handler_node=handler_node,
                            name=route_name,
                            guards=list(dict.fromkeys([*(inherited_guards or []), *guards])),
                        )
        for child in node.named_children:
            visit(child, prefix, name_prefix, inherited_guards)

    visit(root)
    return found


class LaravelParser(PhpParser):
    name = "php-laravel"
    priority = 10
    frameworks = ["laravel"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            b"Illuminate\\Support\\Facades\\Route" in source
            or (b"Illuminate\\Routing\\" in source and b"Route::" in source)
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        record.framework = "laravel"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            detect_laravel_routes(root, ctx.source, ctx.path, record)
        return record
