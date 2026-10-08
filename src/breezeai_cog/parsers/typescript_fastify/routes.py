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
from bisect import bisect_right
from dataclasses import dataclass, field
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
    "catch_clause",
    "for_statement",
    "for_in_statement",
}

_CHAINABLE_METHODS = {"route", "register"}

_PLUGIN_WRAPPER_NAMES = {
    "fp",
    "fastifyPlugin",
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
    "method_definition",
}

_LEXICAL_SCOPE_NODE_TYPES = _SCOPE_NODE_TYPES | _FUNCTION_NODE_TYPES
_SCOPE_LOOKUP_NODE_TYPES = {"call_expression"} | _IDENTIFIER_LIKE_TYPES


@dataclass
class _Binding:
    position: int
    is_fastify: bool = False
    string_value: Optional[str] = None
    callable_node: Optional[Node] = None


@dataclass
class _Scope:
    key: tuple[int, int, str]
    kind: str
    parent: Optional["_Scope"]
    bindings: dict[str, list[_Binding]] = field(default_factory=dict)

    def add_binding(
        self,
        name: str,
        position: int,
        *,
        is_fastify: bool = False,
        string_value: Optional[str] = None,
        callable_node: Optional[Node] = None,
    ) -> None:
        entries = self.bindings.setdefault(name, [])
        entries.insert(
            bisect_right(entries, position, key=lambda entry: entry.position),
            _Binding(position, is_fastify, string_value, callable_node),
        )

    def binding_at(self, name: str, position: int) -> Optional[_Binding]:
        entries = self.bindings.get(name)
        if not entries:
            return None
        index = bisect_right(entries, position, key=lambda entry: entry.position)
        return entries[index - 1] if index else None


def _imported_binding_names(node: Node, source: bytes) -> list[str]:
    clause = next(
        (child for child in node.named_children if child.type == "import_clause"),
        None,
    )
    if clause is None:
        return []

    names: list[str] = []
    for child in clause.named_children:
        if child.type == "identifier":
            names.append(node_text(child, source))
        elif child.type == "namespace_import":
            identifier = next(
                (item for item in child.named_children if item.type == "identifier"),
                None,
            )
            if identifier is not None:
                names.append(node_text(identifier, source))
        elif child.type == "named_imports":
            for specifier in child.named_children:
                if specifier.type == "import_specifier":
                    identifiers = [
                        item for item in specifier.named_children if item.type == "identifier"
                    ]
                    if identifiers:
                        names.append(node_text(identifiers[-1], source))
    return names


class _FileIndex:
    """Per-file lexical scopes and declarations used by route and path lookup."""

    def __init__(self, root: Node, source: bytes, external_constants: dict[str, str]):
        self.external_constants = external_constants
        self.node_scopes: dict[tuple[int, int, str], _Scope] = {}
        self.scopes: dict[tuple[int, int, str], _Scope] = {}
        self.root_scope = self._build(root, source)

    @staticmethod
    def node_key(node: Node) -> tuple[int, int, str]:
        return node.start_byte, node.end_byte, node.type

    def _build(self, root: Node, source: bytes) -> _Scope:
        pending: list[tuple[Node, Optional[_Scope], bool]] = [(root, None, False)]
        root_scope: Optional[_Scope] = None

        while pending:
            node, parent_scope, nested_function = pending.pop()
            scope = parent_scope
            if node.type in _LEXICAL_SCOPE_NODE_TYPES:
                key = self.node_key(node)
                scope = _Scope(key, node.type, parent_scope)
                self.scopes[key] = scope
                if root_scope is None:
                    root_scope = scope

            if scope is None:
                continue

            if node.type in _SCOPE_LOOKUP_NODE_TYPES:
                self.node_scopes[self.node_key(node)] = scope

            if node.type in _FUNCTION_NODE_TYPES:
                self._index_function(node, parent_scope, scope, source, nested_function)

            if node.type == "variable_declarator":
                self._index_variable(node, scope, source)

            if node.type == "import_statement":
                for name in _imported_binding_names(node, source):
                    scope.add_binding(name, 0)

            if node.type in {"function_declaration", "class_declaration"}:
                name_node = node.child_by_field_name("name")
                if name_node is not None and name_node.type == "identifier":
                    destination = scope if node.type == "class_declaration" else parent_scope
                    if destination is not None:
                        is_function = node.type == "function_declaration"
                        destination.add_binding(
                            node_text(name_node, source),
                            # Function declarations are hoisted: visible from scope start.
                            destination.key[0] if is_function else name_node.start_byte,
                            callable_node=node if node.type == "function_declaration" else None,
                        )
            elif node.type in {"function_expression", "function"}:
                name_node = node.child_by_field_name("name")
                if name_node is not None and name_node.type == "identifier":
                    scope.add_binding(node_text(name_node, source), name_node.start_byte)

            if node.type == "catch_clause":
                parameter = node.child_by_field_name("parameter")
                self._index_pattern(parameter, scope, source)

            for child in reversed(node.named_children):
                pending.append(
                    (
                        child,
                        scope,
                        nested_function or node.type in _FUNCTION_NODE_TYPES,
                    )
                )

        if root_scope is None:
            raise ValueError("Fastify route detection requires a scoped syntax tree")
        return root_scope

    def _index_function(
        self,
        node: Node,
        parent_scope: Optional[_Scope],
        function_scope: _Scope,
        source: bytes,
        nested_function: bool,
    ) -> None:
        params = node.child_by_field_name("parameters")
        if params is None:
            return

        plugin = _is_fastify_plugin(node, source, nested=nested_function)
        first_name = _first_param_name(node, source) if plugin else None
        for index, parameter in enumerate(params.named_children):
            pattern = (
                parameter.child_by_field_name("pattern")
                if parameter.type == "required_parameter"
                else parameter
            )
            if pattern is None or pattern.type != "identifier":
                continue
            name = node_text(pattern, source)
            function_scope.add_binding(
                name,
                pattern.start_byte,
                is_fastify=index == 0 and name == first_name,
            )

    def _index_variable(self, node: Node, scope: _Scope, source: bytes) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None or name_node.type != "identifier":
            return

        declaration = node.parent
        destination = scope
        if declaration is not None and declaration.type == "variable_declaration":
            while destination.parent is not None and destination.kind not in (
                "program",
                *_FUNCTION_NODE_TYPES,
            ):
                destination = destination.parent

        value_node = node.child_by_field_name("value")
        destination.add_binding(
            node_text(name_node, source),
            name_node.start_byte,
            is_fastify=(value_node is not None and _is_fastify_factory_call(value_node, source)),
            string_value=(_string_literal(value_node, source) if value_node is not None else None),
            callable_node=(
                value_node
                if value_node is not None
                and value_node.type in {"arrow_function", "function_expression", "function"}
                else None
            ),
        )

    def _index_pattern(
        self,
        node: Optional[Node],
        scope: _Scope,
        source: bytes,
    ) -> None:
        if node is None:
            return
        if node.type == "identifier":
            scope.add_binding(node_text(node, source), node.start_byte)
            return
        for child in node.named_children:
            self._index_pattern(child, scope, source)

    def scope_for(self, node: Node) -> _Scope:
        return self.node_scopes[self.node_key(node)]

    def is_fastify_instance(self, name: str, node: Node) -> bool:
        scope: Optional[_Scope] = self.scope_for(node)
        while scope is not None:
            if name in scope.bindings:
                binding = scope.binding_at(name, node.start_byte)
                return binding.is_fastify if binding is not None else False
            scope = scope.parent
        return False

    def resolve_constant(self, name: str, node: Node) -> Optional[str]:
        scope: Optional[_Scope] = self.scope_for(node)
        while scope is not None:
            if name in scope.bindings:
                binding = scope.binding_at(name, node.start_byte)
                return binding.string_value if binding is not None else None
            scope = scope.parent
        return self.external_constants.get(name)

    def resolve_callable(self, name: str, node: Node) -> Optional[Node]:
        scope: Optional[_Scope] = self.scope_for(node)
        while scope is not None:
            if name in scope.bindings:
                binding = scope.binding_at(name, node.start_byte)
                return binding.callable_node if binding is not None else None
            scope = scope.parent
        return None

    def mark_fastify_parameter(self, function: Node, source: bytes) -> None:
        name = _first_param_name(function, source)
        if name is None:
            return
        scope = self.scopes.get(self.node_key(function))
        if scope is None:
            return
        for entry in scope.bindings.get(name, ()):
            if function.start_byte <= entry.position < function.end_byte:
                entry.is_fastify = True
                return


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

    Known limitations: `require("fastify").default()` factory calls and
    instances assigned after declaration (for example, `let app; app =
    Fastify()`) are not detected.
    """
    # Cheap guard because Fastify detection is additive and runs for every
    # TypeScript/JavaScript file.
    if b"fastify" not in source and b"Fastify" not in source:
        return []

    fid = file_id(path)

    fn_by_name: dict[str, Function] = {
        function.name: function for function in record.functions if function.parentId == fid
    }

    # Reuse values already resolved by the shared resolution index.
    const_values = getattr(resolution_index, "const_values", None)
    external_constants: dict[str, str] = {}
    if isinstance(const_values, dict):
        external_constants = {
            name: value for name, value in const_values.items() if isinstance(value, str)
        }
    file_index = _FileIndex(root, source, external_constants)

    statements: list[Statement] = []

    _walk(
        root,
        source=source,
        path=path,
        fid=fid,
        fn_by_name=fn_by_name,
        file_index=file_index,
        seen_ids=seen_ids,
        out=statements,
        prefix="",
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
    *,
    nested: bool = False,
) -> bool:
    """Return whether a function looks like a Fastify plugin.

    Only top-level plugin-shaped functions should create a Fastify instance
    binding. Nested helper functions are local implementation details and must
    not leak their own `fastify` parameter into sibling or parent scopes.
    """
    params = fn_node.child_by_field_name("parameters")

    if params is None or not params.named_children:
        return False

    if _has_fastify_plugin_type(fn_node, source):
        return True

    if nested:
        return False

    first_param = params.named_children[0]

    name = _first_param_name(fn_node, source)

    if name is None:
        return False

    return name.lower() == "fastify" or _has_fastify_instance_type(first_param, source)


def _has_fastify_plugin_type(
    fn_node: Node,
    source: bytes,
) -> bool:
    """Check whether a function is assigned to a FastifyPlugin-typed variable."""
    node = fn_node.parent

    while node is not None and node.type not in _FUNCTION_NODE_TYPES:
        if node.type == "variable_declarator":
            type_node = node.child_by_field_name("type")
            if type_node is None:
                return False

            stack = [type_node]
            while stack:
                current = stack.pop()
                if current.type == "type_identifier" and node_text(current, source).startswith(
                    "FastifyPlugin"
                ):
                    return True
                stack.extend(current.children)
            return False

        node = node.parent

    return False


def _has_fastify_instance_type(
    node: Node,
    source: bytes,
) -> bool:
    if node.type == "type_identifier":
        return node_text(node, source) == "FastifyInstance"

    return any(_has_fastify_instance_type(child, source) for child in node.children)


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

    if callee.type == "identifier" and node_text(callee, source) in _FASTIFY_FACTORY_NAMES:
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

            if arg_nodes and _string_literal(arg_nodes[0], source) == "fastify":
                return True

    return False


def _walk(
    node: Node,
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict[str, Function],
    file_index: _FileIndex,
    seen_ids: set[str],
    out: list[Statement],
    prefix: Optional[str],
) -> None:
    """Iteratively scan syntax nodes using the prebuilt per-file scope index."""
    mounted_plugin_nodes = _registered_plugin_nodes(node, source, file_index)
    pending: list[tuple[Node, Optional[str], frozenset]] = [(node, prefix, frozenset())]

    while pending:
        current, current_prefix, chain = pending.pop()

        if current.type == "call_expression":
            dispatch = _dispatch_call(current, source, file_index)

            if dispatch is not None:
                kind, member_name, args = dispatch
                # A chained receiver is itself a call that must be scanned.
                chained = current.child_by_field_name("function").child_by_field_name("object")
                if chained.type == "call_expression":
                    pending.append((chained, current_prefix, chain))

                if kind == "method":
                    _http_method_route(
                        current,
                        member_name,
                        args,
                        current_prefix,
                        source=source,
                        path=path,
                        fid=fid,
                        fn_by_name=fn_by_name,
                        seen_ids=seen_ids,
                        out=out,
                        file_index=file_index,
                    )
                    continue

                if kind == "app_route":
                    _app_route(
                        current,
                        args,
                        current_prefix,
                        source=source,
                        path=path,
                        fid=fid,
                        fn_by_name=fn_by_name,
                        seen_ids=seen_ids,
                        out=out,
                        file_index=file_index,
                    )
                    continue

                if kind == "register":
                    children = _fastify_register(
                        current,
                        args,
                        current_prefix,
                        source=source,
                        path=path,
                        fid=fid,
                        fn_by_name=fn_by_name,
                        file_index=file_index,
                        seen_ids=seen_ids,
                        out=out,
                    )
                    # Skip plugins already on the current mount chain so
                    # self- or mutually-registering plugins terminate.
                    pending.extend(
                        (child, child_prefix, chain | {file_index.node_key(child)})
                        for child, child_prefix in reversed(children)
                        if file_index.node_key(child) not in chain
                    )
                    continue

        pending.extend(
            (child, current_prefix, chain)
            for child in reversed(current.named_children)
            if file_index.node_key(child) not in mounted_plugin_nodes
        )


def _registered_plugin_nodes(
    root: Node,
    source: bytes,
    file_index: _FileIndex,
) -> set[tuple[int, int, str]]:
    """Find named plugin declarations so they are visited only at their mount prefix."""
    mounted: set[tuple[int, int, str]] = set()
    pending = [root]

    while pending:
        current = pending.pop()
        if current.type == "call_expression":
            dispatch = _dispatch_call(current, source, file_index)
            if dispatch is not None and dispatch[0] == "register" and dispatch[2]:
                plugin_node = _unwrap_plugin(dispatch[2][0], source)
                if plugin_node.type == "identifier":
                    callable_node = file_index.resolve_callable(
                        node_text(plugin_node, source),
                        plugin_node,
                    )
                    if callable_node is not None:
                        mounted.add(file_index.node_key(callable_node))

        pending.extend(current.named_children)

    return mounted


def _chain_base(node: Optional[Node], source: bytes) -> Optional[Node]:
    """Follow chained Fastify calls (`app.register(a).get(...)`) to the instance."""
    while node is not None and node.type == "call_expression":
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            break
        prop = callee.child_by_field_name("property")
        if prop is None or (
            node_text(prop, source) not in _HTTP_METHODS
            and node_text(prop, source) not in _CHAINABLE_METHODS
        ):
            break
        node = callee.child_by_field_name("object")
    return node


def _dispatch_call(
    node: Node,
    source: bytes,
    file_index: _FileIndex,
) -> Optional[tuple[str, str, list[Node]]]:
    """Classify a Fastify method call."""

    callee = node.child_by_field_name("function")

    if callee is None or callee.type != "member_expression":
        return None

    prop = callee.child_by_field_name("property")

    if prop is None:
        return None

    member_name = node_text(prop, source)

    if member_name not in _HTTP_METHODS and member_name not in {"route", "register"}:
        return None

    receiver = _chain_base(callee.child_by_field_name("object"), source)
    if receiver is None or receiver.type != "identifier":
        return None

    receiver_name = node_text(receiver, source)
    if not file_index.is_fastify_instance(receiver_name, node):
        return None

    args_node = node.child_by_field_name("arguments")
    if args_node is None:
        return None
    args = list(args_node.named_children)

    if member_name in _HTTP_METHODS:
        return "method", member_name, args

    if member_name == "route":
        return "app_route", member_name, args

    return "register", member_name, args


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
    prefix: Optional[str],
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict[str, Function],
    seen_ids: set[str],
    out: list[Statement],
    file_index: _FileIndex,
) -> None:
    """Handle shorthand Fastify route calls."""

    if len(args) < 2:
        return

    url = _resolve_static_string(args[0], source, file_index)

    if url is None or not (url.startswith("/") or url in ("", "*")):
        return

    handler_node = _find_handler_arg(args[1:])

    if handler_node is None:
        logger.debug(
            "fastify: no callable handler argument found for %s %s at %s:%d; handler is unresolved",
            member_name.upper(),
            url,
            path,
            node.start_point[0] + 1,
        )

    methods = list(_ALL_METHODS) if member_name == "all" else [member_name.upper()]

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
    prefix: Optional[str],
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict[str, Function],
    seen_ids: set[str],
    out: list[Statement],
    file_index: _FileIndex,
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

    url = _resolve_static_string(url_node, source, file_index)

    if url is None or not (url.startswith("/") or url in ("", "*")):
        return

    methods: list[str | None]
    if method_node is None:
        methods, dynamic = ["GET"], False
    else:
        methods, dynamic = _method_values(
            method_node,
            source,
            file_index,
        )

    if dynamic:
        logger.debug(
            "fastify: could not statically resolve one or more HTTP "
            "methods for route %s at %s:%d; unresolved method is null",
            url,
            path,
            node.start_point[0] + 1,
        )
        methods.append(None)

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
    file_index: _FileIndex,
) -> tuple[list[str | None], bool]:
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
        methods: list[str | None] = []
        dynamic = False

        for child in method_node.named_children:
            value = _resolve_static_string(child, source, file_index)

            if value:
                methods.append(value.upper())
            else:
                dynamic = True

        return methods, dynamic

    resolved = _resolve_static_string(method_node, source, file_index)

    if resolved:
        return [resolved.upper()], False

    return [], True


def _fastify_register(
    node: Node,
    args: list[Node],
    prefix: Optional[str],
    *,
    source: bytes,
    path: str,
    fid: str,
    fn_by_name: dict[str, Function],
    file_index: _FileIndex,
    seen_ids: set[str],
    out: list[Statement],
) -> list[tuple[Node, Optional[str]]]:
    """Handle `fastify.register(plugin, { prefix })` calls."""

    if not args:
        return []

    plugin_node = _unwrap_plugin(args[0], source)
    opts_node = args[1] if len(args) > 1 else None

    child_prefix = prefix
    mount_endpoint = (prefix or "/") if prefix is not None else None

    if opts_node is not None and opts_node.type == "object":
        pairs = _object_pairs(opts_node, source)
        prefix_node = pairs.get("prefix")

        if prefix_node is not None:
            declared = _resolve_static_string(prefix_node, source, file_index)

            if declared:
                child_prefix = _join_prefix(prefix, declared)
                mount_endpoint = child_prefix
            else:
                child_prefix = None
                mount_endpoint = child_prefix

                logger.debug(
                    "fastify: register() prefix at %s:%d is not a static "
                    "string; route endpoints under this mount are unresolved",
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

    children: list[tuple[Node, Optional[str]]] = []

    if plugin_node.type in (
        "arrow_function",
        "function_expression",
        "function",
    ):
        file_index.mark_fastify_parameter(plugin_node, source)
        children.append((plugin_node, child_prefix))
    elif plugin_node.type == "identifier":
        plugin_name = node_text(plugin_node, source)
        callable_node = file_index.resolve_callable(plugin_name, plugin_node)
        if callable_node is not None:
            file_index.mark_fastify_parameter(callable_node, source)
            children.append((callable_node, child_prefix))

    if opts_node is not None:
        children.append((opts_node, prefix))

    return children


def _unwrap_plugin(node: Node, source: bytes) -> Node:
    """Strip `fp(plugin)` / `fastifyPlugin(plugin)` wrappers around a plugin."""
    while node.type == "call_expression":
        callee = node.child_by_field_name("function")
        call_args = node.child_by_field_name("arguments")
        if (
            callee is None
            or callee.type != "identifier"
            or node_text(callee, source) not in _PLUGIN_WRAPPER_NAMES
            or call_args is None
            or not call_args.named_children
        ):
            break
        node = call_args.named_children[0]
    return node


def _join_prefix(
    prefix: Optional[str],
    suffix: str,
) -> Optional[str]:
    """Combine a prefix and URL into a clean path."""
    if prefix is None:
        return None

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
) -> tuple[str, Optional[str], Optional[int]]:
    """Resolve the function handling a route."""

    if node is None:
        return fid, None, None

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
            None,
            node.start_point[0] + 1,
        )

    return fid, None, node.start_point[0] + 1


def _make_statement(
    node: Node,
    *,
    method: Optional[str],
    endpoint: Optional[str],
    parent_id: str,
    handler: Optional[str],
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
    file_index: _FileIndex,
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
        return file_index.resolve_constant(node_text(node, source), node)

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

        return text[1:-1] if len(text) >= 2 else None

    if node.type == "template_string":
        if any(child.type == "template_substitution" for child in node.children):
            return None

        text = node_text(
            node,
            source,
        )

        return text[1:-1] if len(text) >= 2 else None

    return None
