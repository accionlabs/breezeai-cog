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
from ..php.dto import extract_callable_dtos, extract_use_map
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


def _string_values(node: Node, source: bytes) -> list[str]:
    if node.type == "argument":
        return _string_values(node.named_children[0], source) if node.named_children else []
    if node.type in ("string", "encapsed_string"):
        parts = [c for c in node.named_children if c.type == "string_content"]
        return ["".join(node_text(c, source) for c in parts)] if parts else []
    if node.type == "array_creation_expression":
        values: list[str] = []
        for child in node.named_children:
            value = child.child_by_field_name("value") if child.type == "array_element_initializer" else child
            if value is None and child.type == "array_element_initializer" and child.named_children:
                value = child.named_children[-1]
            if value is not None:
                values.extend(_string_values(value, source))
        return values
    return []


def _middleware_guards(node: Node, source: bytes) -> list[str] | None:
    guards: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in ("member_call_expression", "nullsafe_member_call_expression"):
            name = current.child_by_field_name("name")
            if name is not None and node_text(name, source).lower() == "middleware":
                args_node = current.child_by_field_name("arguments")
                for arg in (args_node.named_children if args_node is not None else []):
                    for value in _string_values(arg, source):
                        if value not in guards:
                            guards.append(value)
        current = current.parent
    return guards or None


def _combine_paths(prefix: str, path: str | None) -> str | None:
    """Join route-group prefixes using CodeIgniter's route path semantics."""
    if not path:
        return f"/{prefix.strip('/')}" if prefix else None
    if not prefix:
        return path
    return f"/{prefix.strip('/')}/{path.lstrip('/')}"


def _enclosing_group_prefix(node: Node, source: bytes) -> str:
    """Return Laravel ``prefix()`` values from enclosing ``group()`` calls."""
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
                call = group_call.child_by_field_name("object")
                group_prefixes: list[str] = []
                while call is not None:
                    name = call.child_by_field_name("name")
                    if name is not None and node_text(name, source).lower() == "prefix":
                        args_node = call.child_by_field_name("arguments")
                        args = list(args_node.named_children) if args_node is not None else []
                        if args and (prefix := _render_url(args[0], source)):
                            group_prefixes.append(prefix)
                    call = call.child_by_field_name("object")
                prefixes.extend(reversed(group_prefixes))
        current = current.parent
    return "/".join(prefix.strip("/") for prefix in reversed(prefixes) if prefix)


def _collect_in_file_classes_and_methods(
    root: Node, source: bytes
) -> tuple[set[str], dict[tuple[str, str], Node]]:
    form_requests: set[str] = set()
    methods: dict[tuple[str, str], Node] = {}

    for child in root.named_children:
        if child.type in ("class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"):
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            cname = node_text(name_node, source)

            base = child.child_by_field_name("base_clause") or child.child_by_field_name("extends")
            if base is not None:
                base_text = node_text(base, source)
                if "FormRequest" in base_text or "Request" in base_text:
                    form_requests.add(cname)

            body = child.child_by_field_name("body")
            if body is not None:
                for elem in body.named_children:
                    if elem.type == "method_declaration":
                        mname_node = elem.child_by_field_name("name")
                        if mname_node is not None:
                            mname = node_text(mname_node, source)
                            methods[(cname, mname)] = elem

    return form_requests, methods


def _resolve_handler_dtos(
    handler_arg: Node | None,
    source: bytes,
    use_map: dict[str, str],
    namespace: str,
    form_requests: set[str],
    methods: dict[tuple[str, str], Node],
) -> tuple[str | None, str | None]:
    if handler_arg is None:
        return None, None

    inner = handler_arg.named_children[0] if handler_arg.type == "argument" and handler_arg.named_children else handler_arg

    if inner.type in ("anonymous_function_creation_expression", "anonymous_function", "arrow_function"):
        return extract_callable_dtos(inner, source, use_map, namespace, form_requests)

    if inner.type == "array_creation_expression":
        items: list[Node] = []
        for c in inner.named_children:
            if c.type == "array_element_initializer":
                val = c.child_by_field_name("value") or (c.named_children[-1] if c.named_children else None)
                if val is not None:
                    items.append(val)
            else:
                items.append(c)
        if len(items) >= 2:
            elem0, elem1 = items[0], items[1]
            cname: str | None = None
            e0_text = node_text(elem0, source)
            if "::" in e0_text:
                lhs, rhs = e0_text.rsplit("::", 1)
                if rhs.strip().lower() == "class":
                    cname = lhs.strip().rsplit("\\", 1)[-1]
            elif elem0.type == "string":
                cname = node_text(elem0, source).strip("'\"").rsplit("\\", 1)[-1]

            mname = node_text(elem1, source).strip("'\"") if elem1.type == "string" else None
            if cname and mname and (cname, mname) in methods:
                return extract_callable_dtos(methods[(cname, mname)], source, use_map, namespace, form_requests)

    if inner.type == "string":
        val = node_text(inner, source).strip("'\"")
        if "@" in val:
            cname, mname = val.split("@", 1)
            cname = cname.rsplit("\\", 1)[-1]
            if (cname, mname) in methods:
                return extract_callable_dtos(methods[(cname, mname)], source, use_map, namespace, form_requests)

    return None, None


def detect_laravel_routes(
    root: Node,
    source: bytes,
    path: str,
    seen_ids: set[str],
) -> list[Statement]:
    """Detect Laravel Route::verb() declarations in a PHP AST."""
    fid = file_id(path)
    routes: list[Statement] = []
    namespace, use_map = extract_use_map(root, source)
    form_requests, methods = _collect_in_file_classes_and_methods(root, source)

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
                        prefix = _enclosing_group_prefix(node, source)
                        guards = _middleware_guards(node, source)

                        if method_name == "match" and len(args) >= 2:
                            # Route::match(['GET', 'POST'], '/path', handler)
                            endpoint = _combine_paths(prefix, _render_url(args[1], source))
                            handler_arg = args[2] if len(args) > 2 else None
                            handler = _handler_text(handler_arg, source)
                            request_dto, response_dto = _resolve_handler_dtos(
                                handler_arg, source, use_map, namespace, form_requests, methods
                            )
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
                                    existing.guards = guards
                                    existing.parentId = owner_id
                                    existing.requestDTO = request_dto
                                    existing.responseDTO = response_dto
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
                                        guards=guards,
                                        requestDTO=request_dto,
                                        responseDTO=response_dto,
                                    )
                                    register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                    routes.append(stmt)
                        elif method_name in ("resource", "apiresource"):
                            endpoint = _combine_paths(prefix, _render_url(args[0], source))
                            handler_arg = args[1] if len(args) > 1 else None
                            handler = _handler_text(handler_arg, source)
                            request_dto, response_dto = _resolve_handler_dtos(
                                handler_arg, source, use_map, namespace, form_requests, methods
                            )
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
                                existing.guards = guards
                                existing.parentId = owner_id
                                existing.requestDTO = request_dto
                                existing.responseDTO = response_dto
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
                                    guards=guards,
                                    requestDTO=request_dto,
                                    responseDTO=response_dto,
                                )
                                register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                routes.append(stmt)
                        else:
                            # Route::get, Route::post, Route::any, etc.
                            endpoint = _combine_paths(prefix, _render_url(args[0], source))
                            handler_arg = args[1] if len(args) > 1 else None
                            handler = _handler_text(handler_arg, source)
                            request_dto, response_dto = _resolve_handler_dtos(
                                handler_arg, source, use_map, namespace, form_requests, methods
                            )
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
                                existing.guards = guards
                                existing.parentId = owner_id
                                existing.requestDTO = request_dto
                                existing.responseDTO = response_dto
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
                                    guards=guards,
                                    requestDTO=request_dto,
                                    responseDTO=response_dto,
                                )
                                register_statement_span(seen_ids, fid, node.start_byte, node.end_byte, stmt)
                                routes.append(stmt)
        for child in node.named_children:
            visit(child)

    visit(root)
    return routes
