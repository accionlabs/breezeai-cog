"""Laravel route detection (Route::get, Route::post, etc.) -> route statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import (
    disambiguate,
    file_id,
    find_statement_by_span,
    register_statement_span,
    statement_id,
)
from ...schemas import Statement
from ..php.owner import owner_id_for_node
from ..statements_common import (
    _extract_verbs,
    _resolve_handler,
    render_concat,
    strip_leading_base,
    url_placeholder,
)
from ..treesitter import node_text

_ROUTE_METHODS = frozenset(
    {
        "get",
        "post",
        "put",
        "delete",
        "patch",
        "options",
        "any",
        "match",
        "resource",
        "apiresource",
    }
)


def _render_url(node: Node, source: bytes) -> str | None:
    if node.type == "argument":
        inner = node.named_children[0] if node.named_children else None
        return _render_url(inner, source) if inner is not None else None
    if node.type == "string":
        frag = next((c for c in node.named_children if c.type == "string_content"), None)
        return node_text(frag, source) if frag is not None else node_text(node, source).strip("'\"")
    if node.type == "encapsed_string":
        parts: list[str] = []
        for c in node.named_children:
            if c.type == "string_content":
                parts.append(node_text(c, source))
            else:
                parts.append(url_placeholder(node_text(c, source).lstrip("$")))
        return strip_leading_base("".join(parts))
    if node.type == "binary_expression":
        return render_concat(node, source, _render_url)
    return None


def _handler_text(arg_node: Node | None, source: bytes) -> str | None:
    return _resolve_handler(arg_node, source)


def detect_laravel_routes(
    root: Node,
    source: bytes,
    path: str,
    seen_ids: set[str],
) -> list[Statement]:
    """Detect Laravel Route::verb() declarations in a PHP AST."""
    fid = file_id(path)
    routes: list[Statement] = []

    def visit(node: Node) -> None:
        if node.type == "scoped_call_expression":
            scope = node.child_by_field_name("scope")
            name_node = node.child_by_field_name("name")
            if scope is not None and name_node is not None:
                scope_name = node_text(scope, source).rsplit("\\", 1)[-1]
                method_name = node_text(name_node, source).lower()
                if scope_name == "Route" and method_name in _ROUTE_METHODS:
                    args_node = node.child_by_field_name("arguments")
                    args = list(args_node.named_children) if args_node is not None else []
                    if args:
                        start, col = node.start_point[0] + 1, node.start_point[1]
                        end = node.end_point[0] + 1
                        owner_id = owner_id_for_node(node, source, path)

                        if method_name == "match" and len(args) >= 2:
                            # Route::match(['GET', 'POST'], '/path', handler)
                            endpoint = _render_url(args[1], source)
                            handler = _handler_text(args[2] if len(args) > 2 else None, source)
                            http_verbs = _extract_verbs(args[0], source) or ["GET"]
                            for verb in http_verbs:
                                existing = find_statement_by_span(
                                    seen_ids, fid, node.start_byte, node.end_byte, node=node
                                )
                                if existing is not None and existing.semanticType is None:
                                    existing.semanticType = "route"
                                    existing.routeKind = "route"
                                    existing.method = verb.upper()
                                    existing.endpoint = endpoint
                                    existing.handler = handler
                                    existing.framework = "laravel"
                                    existing.parentId = owner_id
                                else:
                                    sid = disambiguate(statement_id(path, start, col), seen_ids)
                                    stmt = Statement(
                                        id=sid,
                                        parentId=owner_id,
                                        nodeType=node.type,
                                        semanticType="route",
                                        routeKind="route",
                                        method=verb.upper(),
                                        endpoint=endpoint,
                                        handler=handler,
                                        text=node_text(node, source),
                                        startLine=start,
                                        endLine=end,
                                        path=path,
                                        framework="laravel",
                                    )
                                    register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                    routes.append(stmt)
                        elif method_name in ("resource", "apiresource"):
                            endpoint = _render_url(args[0], source)
                            handler = _handler_text(args[1] if len(args) > 1 else None, source)
                            existing = find_statement_by_span(
                                seen_ids, fid, node.start_byte, node.end_byte, node=node
                            )
                            if existing is not None and existing.semanticType is None:
                                existing.semanticType = "route"
                                existing.routeKind = "route"
                                existing.method = "ANY"
                                existing.endpoint = endpoint
                                existing.handler = handler
                                existing.framework = "laravel"
                                existing.parentId = owner_id
                            else:
                                sid = disambiguate(statement_id(path, start, col), seen_ids)
                                stmt = Statement(
                                    id=sid,
                                    parentId=owner_id,
                                    nodeType=node.type,
                                    semanticType="route",
                                    routeKind="route",
                                    method="ANY",
                                    endpoint=endpoint,
                                    handler=handler,
                                    text=node_text(node, source),
                                    startLine=start,
                                    endLine=end,
                                    path=path,
                                    framework="laravel",
                                )
                                register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                routes.append(stmt)
                        else:
                            # Route::get, Route::post, Route::any, etc.
                            endpoint = _render_url(args[0], source)
                            handler = _handler_text(args[1] if len(args) > 1 else None, source)
                            verb = "ANY" if method_name == "any" else method_name.upper()
                            existing = find_statement_by_span(
                                seen_ids, fid, node.start_byte, node.end_byte, node=node
                            )
                            if existing is not None and existing.semanticType is None:
                                existing.semanticType = "route"
                                existing.routeKind = "route"
                                existing.method = verb
                                existing.endpoint = endpoint
                                existing.handler = handler
                                existing.framework = "laravel"
                                existing.parentId = owner_id
                            else:
                                sid = disambiguate(statement_id(path, start, col), seen_ids)
                                stmt = Statement(
                                    id=sid,
                                    parentId=owner_id,
                                    nodeType=node.type,
                                    semanticType="route",
                                    routeKind="route",
                                    method=verb,
                                    endpoint=endpoint,
                                    handler=handler,
                                    text=node_text(node, source),
                                    startLine=start,
                                    endLine=end,
                                    path=path,
                                    framework="laravel",
                                )
                                register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                routes.append(stmt)
        for child in node.named_children:
            visit(child)

    visit(root)
    return routes
