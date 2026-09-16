"""CodeIgniter route detection (CI4 $routes->verb() and CI3 $route['...']) -> route statements."""

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
    return None


def _handler_text(arg_node: Node | None, source: bytes) -> str | None:
    return _resolve_handler(arg_node, source)


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
                        owner_id = owner_id_for_node(node, source, path)

                        if method_name == "group" and len(args) >= 2:
                            # $routes->group('admin', function($routes) { ... })
                            group_prefix_raw = _render_url(args[0], source) or ""
                            new_prefix = (
                                f"{prefix.rstrip('/')}/{group_prefix_raw.strip('/')}"
                                if prefix
                                else group_prefix_raw
                            )
                            # Traverse children with new prefix
                            closure_types = (
                                "anonymous_function_creation_expression",
                                "anonymous_function",
                                "arrow_function",
                            )
                            closure_node = next(
                                (
                                    a
                                    for a in args
                                    if a.type in closure_types
                                    or (
                                        a.type == "argument"
                                        and any(c.type in closure_types for c in a.named_children)
                                    )
                                ),
                                None,
                            )
                            if closure_node is not None:
                                for child in closure_node.named_children:
                                    visit(child, prefix=new_prefix)
                            return

                        elif method_name == "match" and len(args) >= 2:
                            # $routes->match(['get', 'post'], 'profile', 'Profile::show')
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
                                    existing.method = verb
                                    existing.endpoint = endpoint
                                    existing.handler = handler
                                    existing.framework = "codeigniter"
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
                                        framework="codeigniter",
                                    )
                                    register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                    routes.append(stmt)
                        elif method_name in ("resource", "presenter"):
                            # $routes->resource('photos')
                            endpoint = _combine_paths(prefix, _render_url(args[0], source))
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
                                existing.framework = "codeigniter"
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
                                    framework="codeigniter",
                                )
                                register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                routes.append(stmt)
                        else:
                            # $routes->get, $routes->post, $routes->add, etc.
                            endpoint = _combine_paths(prefix, _render_url(args[0], source))
                            handler = _handler_text(args[1] if len(args) > 1 else None, source)
                            if method_name == "add":
                                verb = "ANY"
                            elif method_name == "cli":
                                verb = "RPC"
                            else:
                                verb = method_name.upper()
                            existing = find_statement_by_span(
                                seen_ids, fid, node.start_byte, node.end_byte, node=node
                            )
                            if existing is not None and existing.semanticType is None:
                                existing.semanticType = "route"
                                existing.routeKind = "route"
                                existing.method = verb
                                existing.endpoint = endpoint
                                existing.handler = handler
                                existing.framework = "codeigniter"
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
                                    framework="codeigniter",
                                )
                                register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                routes.append(stmt)

        # CI3: $route['key'] = 'val' or $route['key']['verb'] = 'val'
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if left is not None and right is not None and left.type == "subscript_expression":
                c0 = left.named_children[0] if left.named_children else None
                c1 = left.named_children[1] if len(left.named_children) > 1 else None
                owner_id = owner_id_for_node(node, source, path)

                # Pattern A: $route['journals'] = 'blogs'
                if (
                    c0 is not None
                    and c0.type == "variable_name"
                    and node_text(c0, source).lstrip("$") == "route"
                    and c1 is not None
                ):
                    key = _render_url(c1, source)
                    if key and key not in _CI3_IGNORED_KEYS:
                        existing = find_statement_by_span(
                            seen_ids, fid, node.start_byte, node.end_byte, node=node
                        )
                        if existing is not None and existing.semanticType is None:
                            existing.semanticType = "route"
                            existing.routeKind = "route"
                            existing.method = "ANY"
                            existing.endpoint = key
                            existing.handler = node_text(right, source).strip("'\"")
                            existing.framework = "codeigniter"
                            existing.parentId = owner_id
                        else:
                            start, col = node.start_point[0] + 1, node.start_point[1]
                            end = node.end_point[0] + 1
                            sid = disambiguate(statement_id(path, start, col), seen_ids)
                            stmt = Statement(
                                id=sid,
                                parentId=owner_id,
                                nodeType=node.type,
                                semanticType="route",
                                routeKind="route",
                                method="ANY",
                                endpoint=key,
                                handler=node_text(right, source).strip("'\""),
                                text=node_text(node, source),
                                startLine=start,
                                endLine=end,
                                path=path,
                                framework="codeigniter",
                            )
                            register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                            routes.append(stmt)

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
                            existing = find_statement_by_span(
                                seen_ids, fid, node.start_byte, node.end_byte, node=node
                            )
                            if existing is not None and existing.semanticType is None:
                                existing.semanticType = "route"
                                existing.routeKind = "route"
                                existing.method = verb_str.upper()
                                existing.endpoint = endpoint
                                existing.handler = node_text(right, source).strip("'\"")
                                existing.framework = "codeigniter"
                                existing.parentId = owner_id
                            else:
                                start, col = node.start_point[0] + 1, node.start_point[1]
                                end = node.end_point[0] + 1
                                sid = disambiguate(statement_id(path, start, col), seen_ids)
                                stmt = Statement(
                                    id=sid,
                                    parentId=owner_id,
                                    nodeType=node.type,
                                    semanticType="route",
                                    routeKind="route",
                                    method=verb_str.upper(),
                                    endpoint=endpoint,
                                    handler=node_text(right, source).strip("'\""),
                                    text=node_text(node, source),
                                    startLine=start,
                                    endLine=end,
                                    path=path,
                                    framework="codeigniter",
                                )
                                register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                routes.append(stmt)

        for child in node.named_children:
            visit(child, prefix=prefix)

    visit(root)
    return routes
