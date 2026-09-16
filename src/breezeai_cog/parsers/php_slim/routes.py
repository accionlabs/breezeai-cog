"""Slim framework route detection ($app->get, $app->post, etc.) -> route statements."""

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

_SLIM_VERBS = frozenset({"get", "post", "put", "delete", "patch", "options", "any", "map"})


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


def _combine_paths(prefix: str, path: str | None) -> str | None:
    """Join route-group prefixes using CodeIgniter's route path semantics."""
    if not path:
        return f"/{prefix.strip('/')}" if prefix else None
    if not prefix:
        return path
    return f"/{prefix.strip('/')}/{path.lstrip('/')}"


def _enclosing_group_prefix(node: Node, source: bytes) -> str:
    """Return the first path argument from every enclosing Slim ``group()``."""
    prefixes: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in ("anonymous_function", "anonymous_function_creation_expression", "arrow_function"):
            argument = current.parent
            arguments = argument.parent if argument is not None else None
            group_call = arguments.parent if arguments is not None else None
            if (
                argument is not None
                and arguments is not None
                and arguments.type == "arguments"
                and group_call is not None
                and group_call.type in ("member_call_expression", "nullsafe_member_call_expression")
                and (name := group_call.child_by_field_name("name")) is not None
                and node_text(name, source).lower() == "group"
            ):
                args = list(arguments.named_children)
                if args and (prefix := _render_url(args[0], source)):
                    prefixes.append(prefix)
        current = current.parent
    return "/".join(prefix.strip("/") for prefix in reversed(prefixes) if prefix)


def detect_slim_routes(
    root: Node,
    source: bytes,
    path: str,
    seen_ids: set[str],
) -> list[Statement]:
    """Detect Slim $app->verb() route declarations."""
    fid = file_id(path)
    routes: list[Statement] = []

    def visit(node: Node) -> None:
        if node.type in ("member_call_expression", "nullsafe_member_call_expression"):
            name_node = node.child_by_field_name("name")
            obj_node = node.child_by_field_name("object")
            if name_node is not None and obj_node is not None:
                method_name = node_text(name_node, source).lower()
                obj_name = node_text(obj_node, source).lower().lstrip("$")
                if method_name in _SLIM_VERBS and obj_name in ("app", "router", "group"):
                    args_node = node.child_by_field_name("arguments")
                    args = list(args_node.named_children) if args_node is not None else []
                    if args:
                        start, col = node.start_point[0] + 1, node.start_point[1]
                        end = node.end_point[0] + 1
                        owner_id = owner_id_for_node(node, source, path)
                        prefix = _enclosing_group_prefix(node, source)

                        if method_name == "map" and len(args) >= 2:
                            # $app->map(['GET', 'POST'], '/path', handler)
                            endpoint = _combine_paths(prefix, _render_url(args[1], source))
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
                                    existing.framework = "slim"
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
                                        framework="slim",
                                    )
                                    register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                    routes.append(stmt)
                        else:
                            endpoint = _combine_paths(prefix, _render_url(args[0], source))
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
                                existing.framework = "slim"
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
                                    framework="slim",
                                )
                                register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                routes.append(stmt)
        for child in node.named_children:
            visit(child)

    visit(root)
    return routes
