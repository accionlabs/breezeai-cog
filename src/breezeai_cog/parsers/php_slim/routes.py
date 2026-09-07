"""Slim framework route detection ($app->get, $app->post, etc.) -> route statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..statements_common import render_concat, strip_leading_base, url_placeholder
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
    if arg_node is None:
        return None
    return node_text(arg_node, source)


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

                        if method_name == "map" and len(args) >= 2:
                            # $app->map(['GET', 'POST'], '/path', handler)
                            endpoint = _render_url(args[1], source)
                            handler = _handler_text(args[2] if len(args) > 2 else None, source)
                            methods_node = args[0]
                            http_verbs: list[str] = []
                            for c in methods_node.named_children:
                                if c.type == "string":
                                    http_verbs.append(_render_url(c, source) or "GET")
                            if not http_verbs:
                                http_verbs = ["GET"]
                            for verb in http_verbs:
                                sid = disambiguate(statement_id(path, start, col), seen_ids)
                                routes.append(
                                    Statement(
                                        id=sid,
                                        parentId=fid,
                                        nodeType=node.type,
                                        semanticType="route",
                                        method=verb.upper(),
                                        endpoint=endpoint,
                                        handler=handler,
                                        text=node_text(node, source),
                                        startLine=start,
                                        endLine=end,
                                        path=path,
                                        framework="slim",
                                    )
                                )
                        else:
                            endpoint = _render_url(args[0], source)
                            handler = _handler_text(args[1] if len(args) > 1 else None, source)
                            verb = "ALL" if method_name == "any" else method_name.upper()
                            sid = disambiguate(statement_id(path, start, col), seen_ids)
                            routes.append(
                                Statement(
                                    id=sid,
                                    parentId=fid,
                                    nodeType=node.type,
                                    semanticType="route",
                                    method=verb,
                                    endpoint=endpoint,
                                    handler=handler,
                                    text=node_text(node, source),
                                    startLine=start,
                                    endLine=end,
                                    path=path,
                                    framework="slim",
                                )
                            )
        for child in node.named_children:
            visit(child)

    visit(root)
    return routes
