"""CodeIgniter route detection (CI4 $routes->verb() and CI3 $route['...']) -> route statements."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..statements_common import render_concat, strip_leading_base, url_placeholder
from ..treesitter import node_text

_CI4_VERBS = frozenset(
    {
        "get",
        "post",
        "put",
        "delete",
        "patch",
        "options",
        "head",
        "cli",
        "add",
        "match",
        "resource",
        "presenter",
        "group",
    }
)

_CI3_IGNORED_KEYS = frozenset(
    {
        "default_controller",
        "404_override",
        "translate_uri_dashes",
    }
)


def _render_url(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
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
    return node_text(node, source).strip("'\"")


def _handler_text(arg_node: Node | None, source: bytes) -> str | None:
    if arg_node is None:
        return None
    return node_text(arg_node, source)


def _combine_paths(prefix: str, path: str | None) -> str | None:
    if not path:
        return f"/{prefix.strip('/')}" if prefix else None
    if not prefix:
        return path
    pfx = prefix.strip("/")
    sub = path.lstrip("/")
    return f"/{pfx}/{sub}"


def detect_codeigniter_routes(
    root: Node,
    source: bytes,
    path: str,
    seen_ids: set[str],
) -> list[Statement]:
    """Detect CodeIgniter 4 ($routes->verb()) and CodeIgniter 3 ($route['...']) route declarations."""
    fid = file_id(path)
    routes: list[Statement] = []

    def visit(node: Node, prefix: str = "") -> None:
        # CI4: $routes->verb(...)
        if node.type in ("member_call_expression", "nullsafe_member_call_expression"):
            name_node = node.child_by_field_name("name")
            obj_node = node.child_by_field_name("object")
            if name_node is not None and obj_node is not None:
                method_name = node_text(name_node, source).lower()
                obj_name = node_text(obj_node, source).lower().lstrip("$")
                if method_name in _CI4_VERBS and (
                    obj_name in ("routes", "route", "router") or obj_name.endswith("routes")
                ):
                    args_node = node.child_by_field_name("arguments")
                    args = list(args_node.named_children) if args_node is not None else []
                    if args:
                        start, col = node.start_point[0] + 1, node.start_point[1]
                        end = node.end_point[0] + 1

                        if method_name == "group" and len(args) >= 2:
                            # $routes->group('admin', function($routes) { ... })
                            group_prefix_raw = _render_url(args[0], source) or ""
                            new_prefix = (
                                f"{prefix.rstrip('/')}/{group_prefix_raw.strip('/')}"
                                if prefix
                                else group_prefix_raw
                            )
                            # Traverse children with new prefix
                            closure_node = args[1]
                            for child in closure_node.named_children:
                                visit(child, prefix=new_prefix)
                            return

                        elif method_name == "match" and len(args) >= 2:
                            # $routes->match(['get', 'post'], 'profile', 'Profile::show')
                            endpoint = _combine_paths(prefix, _render_url(args[1], source))
                            handler = _handler_text(args[2] if len(args) > 2 else None, source)
                            methods_node = args[0]
                            http_verbs: list[str] = []
                            for c in methods_node.named_children:
                                if c.type in ("string", "argument"):
                                    v = _render_url(c, source)
                                    if v:
                                        http_verbs.append(v.upper())
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
                                        method=verb,
                                        endpoint=endpoint,
                                        handler=handler,
                                        text=node_text(node, source),
                                        startLine=start,
                                        endLine=end,
                                        path=path,
                                        framework="codeigniter",
                                    )
                                )
                        elif method_name in ("resource", "presenter"):
                            # $routes->resource('photos')
                            endpoint = _combine_paths(prefix, _render_url(args[0], source))
                            handler = _handler_text(args[1] if len(args) > 1 else None, source)
                            sid = disambiguate(statement_id(path, start, col), seen_ids)
                            routes.append(
                                Statement(
                                    id=sid,
                                    parentId=fid,
                                    nodeType=node.type,
                                    semanticType="route",
                                    method="ALL",
                                    endpoint=endpoint,
                                    handler=handler,
                                    text=node_text(node, source),
                                    startLine=start,
                                    endLine=end,
                                    path=path,
                                    framework="codeigniter",
                                )
                            )
                        else:
                            # $routes->get, $routes->post, $routes->add, etc.
                            endpoint = _combine_paths(prefix, _render_url(args[0], source))
                            handler = _handler_text(args[1] if len(args) > 1 else None, source)
                            verb = "ALL" if method_name == "add" else method_name.upper()
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
                                    framework="codeigniter",
                                )
                            )

        # CI3: $route['key'] = 'val' or $route['key']['verb'] = 'val'
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None and left.type == "subscript_expression":
                c0 = left.named_children[0] if left.named_children else None
                c1 = left.named_children[1] if len(left.named_children) > 1 else None

                # Pattern A: $route['journals'] = 'blogs'
                if (
                    c0 is not None
                    and c0.type == "variable_name"
                    and node_text(c0, source).lstrip("$") == "route"
                    and c1 is not None
                ):
                    key = _render_url(c1, source)
                    if key and key not in _CI3_IGNORED_KEYS:
                        start, col = node.start_point[0] + 1, node.start_point[1]
                        end = node.end_point[0] + 1
                        sid = disambiguate(statement_id(path, start, col), seen_ids)
                        routes.append(
                            Statement(
                                id=sid,
                                parentId=fid,
                                nodeType=node.type,
                                semanticType="route",
                                method="ALL",
                                endpoint=key,
                                handler=node_text(right, source).strip("'\""),
                                text=node_text(node, source),
                                startLine=start,
                                endLine=end,
                                path=path,
                                framework="codeigniter",
                            )
                        )

                # Pattern B: $route['products']['get'] = 'catalog/index'
                elif (
                    c0 is not None
                    and c0.type == "subscript_expression"
                    and c1 is not None
                ):
                    inner_c0 = c0.named_children[0] if c0.named_children else None
                    inner_c1 = c0.named_children[1] if len(c0.named_children) > 1 else None
                    if (
                        inner_c0 is not None
                        and inner_c0.type == "variable_name"
                        and node_text(inner_c0, source).lstrip("$") == "route"
                        and inner_c1 is not None
                    ):
                        endpoint = _render_url(inner_c1, source)
                        verb_str = _render_url(c1, source) or "GET"
                        if endpoint and endpoint not in _CI3_IGNORED_KEYS:
                            start, col = node.start_point[0] + 1, node.start_point[1]
                            end = node.end_point[0] + 1
                            sid = disambiguate(statement_id(path, start, col), seen_ids)
                            routes.append(
                                Statement(
                                    id=sid,
                                    parentId=fid,
                                    nodeType=node.type,
                                    semanticType="route",
                                    method=verb_str.upper(),
                                    endpoint=endpoint,
                                    handler=node_text(right, source).strip("'\""),
                                    text=node_text(node, source),
                                    startLine=start,
                                    endLine=end,
                                    path=path,
                                    framework="codeigniter",
                                )
                            )

        for child in node.named_children:
            visit(child, prefix=prefix)

    visit(root)
    return routes
