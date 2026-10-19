"""CodeIgniter framework parser with CodeIgniter 4 route detection."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..base import ParseContext
from ..php.parser import PhpParser
from ..php_route_common import (
    args_of,
    closure_argument,
    closure_body,
    enrich_route,
    group_filters,
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
    "any": "ANY",
    "add": "ANY"
}


def _group_prefix(call: Node, source: bytes) -> str | None:
    args = args_of(call)
    return string_value(args[0], source) if args else None


def _route_methods(
    method: str | None, args: list[Node], source: bytes
) -> tuple[list[str], int, int | None]:
    if method == "match" and len(args) >= 2:
        return [item.upper() for item in strings_in_array(args[0], source)], 1, 2 if len(args) > 2 else None
    if method in _VERBS:
        return [_VERBS[method]], 0, 1 if len(args) > 1 else None
    return [], 0, None


def detect_codeigniter_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    statements = {statement.id: statement for statement in record.statements}
    seen_ids = {statement.id for statement in record.statements}
    found = False

    def visit(
        node: Node, prefix: str = "", guards: list[str] | None = None
    ) -> None:
        nonlocal found
        if node.type == "member_call_expression":
            method = method_of(node, source)
            args = args_of(node)
            if method == "group":
                closure = closure_argument(node)
                if closure is not None:
                    group_prefix = _group_prefix(node, source)
                    group_guards = group_filters(args[1] if len(args) > 1 else None, source)
                    visit(
                        closure_body(closure),
                        join_endpoint(prefix, group_prefix or "") if group_prefix else prefix,
                        list(dict.fromkeys([*(guards or []), *group_guards])),
                    )
                    return
            methods, path_index, handler_index = _route_methods(method, args, source)
            endpoint = string_value(args[path_index], source) if len(args) > path_index else None
            receiver = node.child_by_field_name("object")
            receiver_name = node_text(receiver, source).lstrip("$").rsplit("->", 1)[-1] if receiver else ""
            if endpoint is not None and receiver_name.lower() in {"routes", "routecollection"}:
                for verb in methods:
                    found |= enrich_route(
                        node, source, path, statements, record.statements, seen_ids,
                        framework="codeigniter",
                        endpoint=join_endpoint(prefix, endpoint), method=verb,
                        handler_node=args[handler_index] if handler_index is not None else None,
                        guards=guards,
                        is_regex=bool("(:num)" in endpoint or "(:segment)" in endpoint or "(:any)" in endpoint or "(.*)" in
                        endpoint)                    )
        for child in node.named_children:
            visit(child, prefix, guards)

    visit(root)
    return found


class CodeIgniterParser(PhpParser):
    name = "php-codeigniter"
    priority = 20
    frameworks = ["codeigniter"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"CodeIgniter\\" in source or b"CodeIgniter\\Router\\RouteCollection" in source

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        record.framework = "codeigniter"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            detect_codeigniter_routes(root, ctx.source, ctx.path, record)
        return record
