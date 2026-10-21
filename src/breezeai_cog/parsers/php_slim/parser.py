"""Slim framework parser with literal-path route detection."""

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
}


def detect_slim_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    statements = {statement.id: statement for statement in record.statements}
    seen_ids = {statement.id for statement in record.statements}
    found = False

    def visit(node: Node, prefix: str = "") -> None:
        nonlocal found
        if node.type in {"member_call_expression", "scoped_call_expression"}:
            method = method_of(node, source)
            args = args_of(node)
            if method == "group":
                closure = closure_argument(node)
                if closure is not None:
                    base = string_value(args[0], source) if args else None
                    if base is None:
                        base = chain_prefix(node.child_by_field_name("object"), source)
                    visit(closure_body(closure), join_endpoint(prefix, base or "") if base else prefix)
                    return
            if node.type == "member_call_expression" and method == "map" and len(args) >= 2:
                methods = [m.upper() for m in strings_in_array(args[0], source)]
                endpoint_node = args[1]
                handler_node = args[2] if len(args) > 2 else None
            elif method in _VERBS and args:
                methods = [_VERBS[method]]
                endpoint_node = args[0]
                handler_node = args[1] if len(args) > 1 else None
            else:
                methods = []
                endpoint_node, handler_node = None, None
            endpoint = string_value(endpoint_node, source)
            receiver = node.child_by_field_name("object")
            receiver_name = node_text(receiver, source).lstrip("$").rsplit("->", 1)[-1] if receiver else ""
            if endpoint is not None and receiver_name.lower() in {
                "app", "group", "router", "routecollector",
            }:
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
                        endpoint=join_endpoint(prefix, endpoint), method=verb,
                        handler_node=handler_node,
                        name=route_name,
                        guards=guards,
                    )
        for child in node.named_children:
            visit(child, prefix)

    visit(root)
    return found


class SlimParser(PhpParser):
    name = "php-slim"
    priority = 30
    frameworks = ["slim"]

    def claims(self, path: str, source: bytes) -> bool:
        explicit_marker = b"Slim\\" in source
        normalized_path = path.lower().replace("\\", "/")
        entrypoint = normalized_path.endswith(
            ("/public/index.php", "/index.php", "/routes.php")
        )
        slim_route_call = b"$app" in source and any(
            token in source for token in (
                b"->get", b"->post", b"->put", b"->patch", b"->delete",
                b"->options", b"->map", b"->run",
            )
        )
        other_framework = any(
            marker in source for marker in (b"CodeIgniter\\", b"Illuminate\\", b"Symfony\\")
        )
        return explicit_marker or (entrypoint and slim_route_call and not other_framework)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        record.framework = "slim"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            detect_slim_routes(root, ctx.source, ctx.path, record)
        return record
