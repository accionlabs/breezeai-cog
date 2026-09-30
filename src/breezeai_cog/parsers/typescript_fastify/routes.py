"""Fastify route detection. Fastify is call-based — routes are registered
by calling an HTTP-verb method on a Fastify instance:

  fastify.get('/users/:id', handler)   fastify.post('/users', handler)   → route
  fastify.register(plugin, { prefix })                                  → route (mount)
  fastify.route({ method, url, handler })                               → route

Per the capture contract, detection enriches the owning parser's statements
in place. Fastify detection is additive and is invoked from
``TypeScriptParser.extract`` for every TS/JS file, allowing Fastify routes to
coexist with routes detected by other TypeScript framework parsers.

Mutates ``record`` (mirrors the other call-based route detectors).
"""

from __future__ import annotations

import logging
from typing import Optional

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Function, Statement
from ..treesitter import node_text


__all__ = ["detect_fastify_routes"]

logger = logging.getLogger(__name__)


_HTTP_METHODS = {
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "options",
    "head",
    "all",
}

_ALL_METHODS = [
    "GET",
    "POST",
    "PUT",
    "DELETE",
    "PATCH",
    "OPTIONS",
    "HEAD",
]

_CALLABLE_NODE_TYPES = {
    "arrow_function",
    "function_expression",
    "function",
    "identifier",
    "member_expression",
}

_STRING_NODE_TYPES = {
    "string",
    "template_string",
}

_SHORTHAND_PROPERTY_TYPES = {
    "shorthand_property_identifier",
    "shorthand_property_identifier_pattern",
}

_IDENTIFIER_LIKE_TYPES = {
    "identifier",
} | _SHORTHAND_PROPERTY_TYPES

_SCOPE_NODE_TYPES = {
    "program",
    "statement_block",
}

_FASTIFY_FACTORY_NAMES = {
    "fastify",
    "Fastify",
}

_FUNCTION_NODE_TYPES = {
    "function_declaration",
    "function_expression",
    "arrow_function",
    "function",
}

_MAX_WALK_DEPTH = 900


def _referenced_identifiers(node: Node, source: bytes) -> set[str]:
    if node.type in _FUNCTION_NODE_TYPES:
        return set()
    if node.type == "identifier":
        return {node_text(node, source)}

    names: set[str] = set()
    for child in node.named_children:
        names.update(_referenced_identifiers(child, source))
    return names


def _exported_binding_names(root: Node, source: bytes) -> set[str]:
    names: set[str] = set()
    stack = [root]

    while stack:
        node = stack.pop()

        if node.type == "export_statement":
            for child in node.named_children:
                names.update(_referenced_identifiers(child, source))
        elif node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")

            if (
                left is not None
                and right is not None
                and node_text(left, source) == "module.exports"
            ):
                names.update(_referenced_identifiers(right, source))

        stack.extend(node.children)

    return names


def detect_fastify_routes(
    root: Node,
    source: bytes,
    path: str,
    record: FileRecord,
    *,
    seen_ids: set[str],
    resolution_index: object | None = None,
) -> list[Statement]:
    """Find Fastify routes and return them as Statements.

    Detects shorthand routes, `route()` configurations, and `register()`
    mounts, including inherited prefixes and resolvable variable values.
    """
    # Cheap guard because Fastify detection is additive and runs for every
    # TypeScript/JavaScript file.
    if b"fastify" not in source.lower():
        return []

    fid = file_id(path)

    fn_by_name: dict[str, Function] = {
        function.name: function
        for function in record.functions
        if function.parentId == fid
    }

    fastify_instance_names: set[str] = set()
    resolution_cache: dict[
        tuple[int, str],
        Optional[str],
    ] = {}

    # Reuse values already resolved by the shared resolution index.
    const_values = getattr(resolution_index, "const_values", None)

    if isinstance(const_values, dict):
        resolution_cache.update(
            {
                (-2, name): value
                for name, value in const_values.items()
                if isinstance(value, str)
            }
        )

    statements: list[Statement] = []

    _walk(
        root,
        source=source,
        path=path,
        fid=fid,
        fn_by_name=fn_by_name,
        fastify_instance_names=fastify_instance_names,
        seen_ids=seen_ids,
        out=statements,
        prefix="",
        resolution_cache=resolution_cache,
    )

    return statements


def _first_param_name(
    fn_node: Node,
    source: bytes,
) -> Optional[str]:
    """Return the name of a function's first parameter.

    Only simple identifier parameters are supported.
    """
    params = fn_node.child_by_field_name("parameters")

    if params is None:
        return None

    for child in params.named_children:
        if child.type == "identifier":
            return node_text(child, source)

        if child.type == "required_parameter":
            name = child.child_by_field_name("pattern")

            if name is not None and name.type == "identifier":
                return node_text(name, source)

        # The first parameter exists but is not a simple identifier.
        return None

    return None


def _is_fastify_plugin(
    fn_node: Node,
    source: bytes,
) -> bool:
    """Return whether a function looks like a Fastify plugin.

    Only top-level plugin-shaped functions should create a Fastify instance
    binding. Nested helper functions are local implementation details and must
    not leak their own `fastify` parameter into sibling or parent scopes.
    """
    parent = fn_node.parent
    while parent is not None:
        if parent.type in _FUNCTION_NODE_TYPES:
            return False
        parent = parent.parent

    params = fn_node.child_by_field_name("parameters")

    if params is None or not params.named_children:
        return False

    first_param = params.named_children[0]

    name = _first_param_name(fn_node, source)

    if name is None:
        return False

    return (
        name.lower() == "fastify"
        or _has_fastify_instance_type(first_param, source)
    )


def _has_fastify_instance_type(
    node: Node,
    source: bytes,
) -> bool:
    if node.type == "type_identifier":
        return node_text(node, source) == "FastifyInstance"

    return any(
        _has_fastify_instance_type(child, source)
        for child in node.children
    )


def _is_fastify_factory_call(
    value_node: Node,
    source: bytes,
) -> bool:
    """Check whether a value is created by a Fastify factory call.

    Supports:

        Fastify()
        fastify()
        require("fastify")()
    """
    if value_node.type != "call_expression":
        return False

    callee = value_node.child_by_field_name("function")

    if callee is None:
        return False

    if (
        callee.type == "identifier"
        and node_text(callee, source) in _FASTIFY_FACTORY_NAMES
    ):
        return True

    if callee.type == "call_expression":
        inner_callee = callee.child_by_field_name("function")
        inner_args = callee.child_by_field_name("arguments")

        if (
            inner_callee is not None
            and node_text(inner_callee, source) == "require"
            and inner_args is not None
        ):
            arg_nodes = list(inner_args.named_children)

            if (
                arg_nodes
                and _string_literal(arg_nodes[0], source) == "fastify"
            ):
                return True

    return False


def _add_fastify_instance_name(
    node: Node,
    source: bytes,
    names: set[str],
) -> None:
    """Add variables initialized from a Fastify factory."""
    if node.type != "variable_declarator":
        return

    name_node = node.child_by_field_name("name")
    value_node = node.child_by_field_name("value")

    if (
        name_node is not None
        and name_node.type == "identifier"
        and value_node is not None
        and _is_fastify_factory_call(value_node, source)
    ):
        names.add(node_text(name_node, source))


def _walk(
    node: Node,
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict[str, Function],
    fastify_instance_names: set[str],
    seen_ids: set[str],
    out: list[Statement],
    prefix: str,
    resolution_cache: dict[tuple[int, str], Optional[str]],
    depth: int = 0,
) -> None:
    """Recursively scan the syntax tree for Fastify route calls."""

    if depth >= _MAX_WALK_DEPTH:
        return

    if node.type in (
        "function_declaration",
        "function_expression",
        "arrow_function",
    ):
        # Each function gets its own copy so that a nested plugin's
        # Fastify parameter does not leak into unrelated functions.
        fastify_instance_names = set(fastify_instance_names)

        if _is_fastify_plugin(node, source):
            plugin_param = _first_param_name(node, source)
            if plugin_param:
                fastify_instance_names.add(plugin_param)

    _add_fastify_instance_name(
        node,
        source,
        fastify_instance_names,
    )

    if node.type == "call_expression":
        dispatch = _dispatch_call(
            node,
            source,
            fastify_instance_names,
        )

        if dispatch is not None:
            kind, member_name, args = dispatch

            if kind == "method":
                _http_method_route(
                    node,
                    member_name,
                    args,
                    prefix,
                    source=source,
                    path=path,
                    fid=fid,
                    fn_by_name=fn_by_name,
                    seen_ids=seen_ids,
                    out=out,
                    resolution_cache=resolution_cache,
                )
                return

            if kind == "app_route":
                _app_route(
                    node,
                    args,
                    prefix,
                    source=source,
                    path=path,
                    fid=fid,
                    fn_by_name=fn_by_name,
                    seen_ids=seen_ids,
                    out=out,
                    resolution_cache=resolution_cache,
                )
                return

            if kind == "register":
                _fastify_register(
                    node,
                    args,
                    prefix,
                    source=source,
                    path=path,
                    fid=fid,
                    fn_by_name=fn_by_name,
                    fastify_instance_names=fastify_instance_names,
                    seen_ids=seen_ids,
                    out=out,
                    depth=depth,
                    resolution_cache=resolution_cache,
                )
                return

    for child in node.children:
        _walk(
            child,
            source=source,
            path=path,
            fid=fid,
            fn_by_name=fn_by_name,
            fastify_instance_names=fastify_instance_names,
            seen_ids=seen_ids,
            out=out,
            prefix=prefix,
            depth=depth + 1,
            resolution_cache=resolution_cache,
        )


def _dispatch_call(
    node: Node,
    source: bytes,
    fastify_instance_names: set[str],
) -> Optional[tuple[str, str, list[Node]]]:
    """Classify a Fastify method call."""

    callee = node.child_by_field_name("function")

    if callee is None or callee.type != "member_expression":
        return None

    prop = callee.child_by_field_name("property")

    if prop is None:
        return None

    member_name = node_text(prop, source)

    args_node = node.child_by_field_name("arguments")

    if args_node is None:
        return None

    args = list(args_node.named_children)

    receiver = callee.child_by_field_name("object")

    if receiver is None or receiver.type != "identifier":
        return None

    receiver_name = node_text(receiver, source)

    if receiver_name not in fastify_instance_names:
        return None

    if member_name in _HTTP_METHODS:
        return "method", member_name, args

    if member_name == "route":
        return "app_route", member_name, args

    if member_name == "register":
        return "register", member_name, args

    return None


def _find_handler_arg(
    trailing_args: list[Node],
) -> Optional[Node]:
    """Find the most likely route handler.

    Checks arguments from right to left because the handler is normally
    the last argument.
    """
    for candidate in reversed(trailing_args):
        if candidate.type in _CALLABLE_NODE_TYPES:
            return candidate

    return None


def _http_method_route(
    node: Node,
    member_name: str,
    args: list[Node],
    prefix: str,
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict[str, Function],
    seen_ids: set[str],
    out: list[Statement],
    resolution_cache: dict[tuple[int, str], Optional[str]],
) -> None:
    """Handle shorthand Fastify route calls."""

    if len(args) < 2:
        return

    url = _resolve_static_string(
        args[0],
        source,
        resolution_cache,
    )

    if url is None or not url.startswith("/"):
        return

    handler_node = _find_handler_arg(args[1:])

    if handler_node is None:
        logger.debug(
            "fastify: no callable handler argument found for %s %s at "
            "%s:%d; recording it with handler=UNKNOWN",
            member_name.upper(),
            url,
            path,
            node.start_point[0] + 1,
        )

    methods = (
        list(_ALL_METHODS)
        if member_name == "all"
        else [member_name.upper()]
    )

    route = _join_prefix(prefix, url)

    parent_id, handler_name, handler_line = _resolve_handler(
        handler_node,
        source,
        fid=fid,
        fn_by_name=fn_by_name,
    )

    for method in methods:
        out.append(
            _make_statement(
                node,
                method=method,
                endpoint=route,
                parent_id=parent_id,
                handler=handler_name,
                handler_line=handler_line,
                route_kind="route",
                path=path,
                source=source,
                seen_ids=seen_ids,
            )
        )


def _app_route(
    node: Node,
    args: list[Node],
    prefix: str,
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict[str, Function],
    seen_ids: set[str],
    out: list[Statement],
    resolution_cache: dict[tuple[int, str], Optional[str]],
) -> None:
    """Handle `fastify.route({...})` calls."""

    if not args or args[0].type != "object":
        return

    pairs = _object_pairs(
        args[0],
        source,
    )

    method_node = pairs.get("method")
    url_node = pairs.get("url") or pairs.get("path")
    handler_node = pairs.get("handler")

    if url_node is None:
        return

    url = _resolve_static_string(
        url_node,
        source,
        resolution_cache,
    )

    if url is None or not url.startswith("/"):
        return

    if method_node is None:
        methods, dynamic = ["GET"], False
    else:
        methods, dynamic = _method_values(
            method_node,
            source,
            resolution_cache,
        )

    if dynamic:
        logger.debug(
            "fastify: could not statically resolve one or more HTTP "
            "methods for route %s at %s:%d",
            url,
            path,
            node.start_point[0] + 1,
        )
        methods.append("UNKNOWN")

    if not methods:
        return

    route = _join_prefix(prefix, url)

    parent_id, handler_name, handler_line = _resolve_handler(
        handler_node,
        source,
        fid=fid,
        fn_by_name=fn_by_name,
    )

    for method in methods:
        out.append(
            _make_statement(
                node,
                method=method,
                endpoint=route,
                parent_id=parent_id,
                handler=handler_name,
                handler_line=handler_line,
                route_kind="route",
                path=path,
                source=source,
                seen_ids=seen_ids,
            )
        )


def _method_values(
    method_node: Node,
    source: bytes,
    resolution_cache: dict[tuple[int, str], Optional[str]],
) -> tuple[list[str], bool]:
    """Resolve HTTP method values.

    Supports:
        method: "GET"
        method: ["GET", "POST"]
        method: METHOD
    """
    if method_node.type in _STRING_NODE_TYPES:
        value = _string_literal(
            method_node,
            source,
        )

        return (
            [value.upper()] if value else [],
            False,
        )

    if method_node.type == "array":
        methods: list[str] = []
        dynamic = False

        for child in method_node.named_children:
            value = _resolve_static_string(
                child,
                source,
                resolution_cache,
            )

            if value:
                methods.append(value.upper())
            else:
                dynamic = True

        return methods, dynamic

    resolved = _resolve_static_string(
        method_node,
        source,
        resolution_cache,
    )

    if resolved:
        return [resolved.upper()], False

    return [], True


def _fastify_register(
    node: Node,
    args: list[Node],
    prefix: str,
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict[str, Function],
    fastify_instance_names: set[str],
    seen_ids: set[str],
    out: list[Statement],
    depth: int,
    resolution_cache: dict[tuple[int, str], Optional[str]],
) -> None:
    """Handle `fastify.register(plugin, { prefix })` calls."""

    if not args:
        return

    plugin_node = args[0]
    opts_node = args[1] if len(args) > 1 else None

    child_prefix = prefix
    mount_endpoint = prefix or "/"

    if opts_node is not None and opts_node.type == "object":
        pairs = _object_pairs(opts_node, source)
        prefix_node = pairs.get("prefix")

        if prefix_node is not None:
            declared = _resolve_static_string(
                prefix_node,
                source,
                resolution_cache,
            )

            if declared:
                child_prefix = _join_prefix(prefix, declared)
                mount_endpoint = child_prefix
            else:
                child_prefix = _join_prefix(
                    prefix,
                    "<unresolved-prefix>",
                )
                mount_endpoint = child_prefix

                logger.debug(
                    "fastify: register() prefix at %s:%d is not a static "
                    "string; using '<unresolved-prefix>'",
                    path,
                    node.start_point[0] + 1,
                )

    parent_id, handler_name, handler_line = _resolve_handler(
        plugin_node,
        source,
        fid=fid,
        fn_by_name=fn_by_name,
    )

    out.append(
        _make_statement(
            node,
            method=None,
            endpoint=mount_endpoint,
            parent_id=parent_id,
            handler=handler_name,
            handler_line=handler_line,
            route_kind="mount",
            path=path,
            source=source,
            seen_ids=seen_ids,
        )
    )

    # Inline plugin:
    # fastify.register(async (childFastify) => {
    #     childFastify.get("/users", handler)
    # })
    if plugin_node.type in (
        "arrow_function",
        "function_expression",
        "function",
    ):
        child_instance_names = set(fastify_instance_names)

        plugin_param = _first_param_name(
            plugin_node,
            source,
        )

        if plugin_param:
            child_instance_names.add(plugin_param)

        _walk(
            plugin_node,
            source=source,
            path=path,
            fid=fid,
            fn_by_name=fn_by_name,
            fastify_instance_names=child_instance_names,
            seen_ids=seen_ids,
            out=out,
            prefix=child_prefix,
            depth=depth + 1,
            resolution_cache=resolution_cache,
        )

    if opts_node is not None:
        _walk(
            opts_node,
            source=source,
            path=path,
            fid=fid,
            fn_by_name=fn_by_name,
            fastify_instance_names=fastify_instance_names,
            seen_ids=seen_ids,
            out=out,
            prefix=prefix,
            depth=depth + 1,
            resolution_cache=resolution_cache,
        )


def _join_prefix(
    prefix: str,
    suffix: str,
) -> str:
    """Combine a prefix and URL into a clean path."""
    prefix = prefix.rstrip("/")

    if not suffix or suffix == "/":
        return prefix or "/"

    if not suffix.startswith("/"):
        suffix = "/" + suffix

    return f"{prefix}{suffix}"


def _resolve_handler(
    node: Optional[Node],
    source: bytes,
    *,
    fid: str,
    fn_by_name: dict[str, Function],
) -> tuple[str, str, Optional[int]]:
    """Resolve the function handling a route."""

    if node is None:
        return fid, "<unknown>", None

    if node.type == "identifier":
        name = node_text(
            node,
            source,
        )

        fn = fn_by_name.get(name)

        if fn is not None:
            return fn.id, name, fn.startLine

        return (
            fid,
            name,
            node.start_point[0] + 1,
        )

    if node.type == "member_expression":
        return (
            fid,
            node_text(node, source),
            node.start_point[0] + 1,
        )

    if node.type in {
        "arrow_function",
        "function_expression",
        "function",
    }:
        return (
            fid,
            "<inline>",
            node.start_point[0] + 1,
        )

    return (
        fid,
        node_text(node, source)[:80],
        node.start_point[0] + 1,
    )


def _make_statement(
    node: Node,
    *,
    method: Optional[str],
    endpoint: str,
    parent_id: str,
    handler: str,
    handler_line: Optional[int],
    route_kind: str,
    path: str,
    source: bytes,
    seen_ids: set[str],
) -> Statement:
    """Create a Statement for a Fastify route or plugin mount."""

    start_line = node.start_point[0] + 1
    start_column = node.start_point[1]

    return Statement(
        id=disambiguate(
            statement_id(
                path,
                start_line,
                start_column,
            ),
            seen_ids,
        ),
        parentId=parent_id,
        semanticType="route",
        method=method,
        endpoint=endpoint,
        routeKind=route_kind,
        nodeType="synthetic",
        text=node_text(node, source).split("\n", 1)[0],
        framework="fastify",
        handler=handler,
        handlerLine=handler_line,
        isRegex=False,
        authRequired=None,
        guards=None,
        startLine=start_line,
        endLine=node.end_point[0] + 1,
        path=path,
    )


def _object_pairs(
    obj_node: Node,
    source: bytes,
) -> dict[str, Node]:
    """Convert an object literal into a key/value lookup table."""

    pairs: dict[str, Node] = {}

    for child in obj_node.named_children:
        if child.type == "pair":
            key_node = child.child_by_field_name("key")
            value_node = child.child_by_field_name("value")

            if key_node is None or value_node is None:
                continue

            key_text = node_text(
                key_node,
                source,
            ).strip("'\"`")

            pairs[key_text] = value_node

        elif child.type in _SHORTHAND_PROPERTY_TYPES:
            pairs[node_text(child, source)] = child

    return pairs


def _resolve_static_string(
    node: Node,
    source: bytes,
    resolution_cache: dict[tuple[int, str], Optional[str]],
) -> Optional[str]:
    """Resolve a node into a string value.

    Supports plain strings, static template literals, local variables,
    and values supplied by the shared resolution index.
    """
    literal = _string_literal(
        node,
        source,
    )

    if literal is not None:
        return literal

    if node.type in _IDENTIFIER_LIKE_TYPES:
        name = node_text(
            node,
            source,
        )

        scope = node.parent

        while (
            scope is not None
            and scope.type not in _SCOPE_NODE_TYPES
        ):
            scope = scope.parent

        key = (
            scope.start_byte if scope is not None else -1,
            name,
        )

        if key not in resolution_cache:
            resolution_cache[key] = _resolve_in_enclosing_scope(
                node,
                name,
                source,
            )

        resolved = resolution_cache[key]

        if resolved is not None:
            return resolved

        # (-2, name) contains values supplied by the shared
        # resolution index.
        return resolution_cache.get(
            (-2, name)
        )

    return None


def _resolve_in_enclosing_scope(
    node: Node,
    name: str,
    source: bytes,
) -> Optional[str]:
    """Look up a variable through its surrounding function scopes."""

    scope = node.parent

    while scope is not None:
        if scope.type in _SCOPE_NODE_TYPES:
            value = _lookup_const_in_block(
                scope,
                name,
                source,
            )

            if value is not None:
                return value

        scope = scope.parent

    return None


def _lookup_const_in_block(
    block: Node,
    name: str,
    source: bytes,
) -> Optional[str]:
    """Find a variable declared directly in a code block."""

    for child in block.named_children:
        if child.type not in {
            "lexical_declaration",
            "variable_declaration",
        }:
            continue

        for declarator in child.named_children:
            if declarator.type != "variable_declarator":
                continue

            name_node = declarator.child_by_field_name("name")
            value_node = declarator.child_by_field_name("value")

            if (
                name_node is not None
                and name_node.type == "identifier"
                and node_text(name_node, source) == name
                and value_node is not None
            ):
                literal = _string_literal(
                    value_node,
                    source,
                )

                if literal is not None:
                    return literal

    return None


def _string_literal(
    node: Node,
    source: bytes,
) -> Optional[str]:
    """Read a plain or static template string.

    Dynamic template literals return None.
    """
    if node.type == "string":
        text = node_text(
            node,
            source,
        )

        return (
            text[1:-1]
            if len(text) >= 2
            else None
        )

    if node.type == "template_string":
        if any(
            child.type == "template_substitution"
            for child in node.children
        ):
            return None

        text = node_text(
            node,
            source,
        )

        return (
            text[1:-1]
            if len(text) >= 2
            else None
        )

    return None