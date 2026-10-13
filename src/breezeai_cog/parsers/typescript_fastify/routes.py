"""Fastify route detection. Fastify is call-based — routes are registered
by calling an HTTP-verb method on a Fastify instance:

  fastify.get('/users/:id', handler)   fastify.post('/users', handler)   → route
  fastify.register(plugin, { prefix })                                  → route (mount)
  fastify.route({ method, url, handler })                               → route

Per the capture contract, detection enriches the owning parser's statements
in place. Fastify detection is registered as an additive detector and runs
through the detector registry for TypeScript files, allowing Fastify routes to
coexist with routes detected by other TypeScript framework parsers.

Mutates ``record`` (mirrors the other call-based route detectors).
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Optional

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Function, Statement
from ..additive import DetectContext, Detector, register_detector
from ..enclosing import Enclosing
from ..statements_common import render_template_url
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
    "TRACE",
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
_FASTIFY_CALL_METHODS = _HTTP_METHODS | _CHAINABLE_METHODS

_ROUTE_HOOKS = ("onRequest", "preValidation", "preHandler")

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
_NON_EXECUTABLE_NODE_TYPES = {
    "import_statement",
    "export_clause",
    "interface_declaration",
    "type_alias_declaration",
    "comment",
}


@dataclass
class _Binding:
    position: int
    is_fastify: bool = False
    string_value: Optional[str] = None
    callable_node: Optional[Node] = None
    is_fastify_factory: bool = False
    factory_call: Optional[Node] = None


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
        is_fastify_factory: bool = False,
        factory_call: Optional[Node] = None,
    ) -> None:
        entries = self.bindings.setdefault(name, [])
        entries.insert(
            bisect_right(entries, position, key=lambda entry: entry.position),
            _Binding(
                position,
                is_fastify,
                string_value,
                callable_node,
                is_fastify_factory,
                factory_call,
            ),
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


def _imported_fastify_factory_names(node: Node, source: bytes) -> set[str]:
    source_node = node.child_by_field_name("source")
    if source_node is None or _string_literal(source_node, source) != "fastify":
        return set()

    clause = next(
        (child for child in node.named_children if child.type == "import_clause"),
        None,
    )
    if clause is None:
        return set()

    names: set[str] = set()
    for child in clause.named_children:
        if child.type == "identifier":
            names.add(node_text(child, source))
        elif child.type == "namespace_import":
            identifier = next(
                (item for item in child.named_children if item.type == "identifier"),
                None,
            )
            if identifier is not None:
                names.add(node_text(identifier, source))
        elif child.type == "named_imports":
            for specifier in child.named_children:
                if specifier.type != "import_specifier":
                    continue
                imported = specifier.child_by_field_name("name")
                local = specifier.child_by_field_name("alias") or imported
                if (
                    imported is not None
                    and local is not None
                    and node_text(imported, source) in _FASTIFY_FACTORY_NAMES
                ):
                    names.add(node_text(local, source))
    return names


class _FileIndex:
    """Per-file lexical scopes and declarations used by route and path lookup."""

    def __init__(self, root: Node, source: bytes, external_constants: dict[str, str]):
        self.external_constants = external_constants
        self.node_scopes: dict[tuple[int, int, str], _Scope] = {}
        self.scopes: dict[tuple[int, int, str], _Scope] = {}
        self.register_calls: list[Node] = []
        self.call_nodes: list[Node] = []
        self._class_factory_fields: list[tuple[tuple[int, int, str], str, Node]] = []
        self.fastify_class_fields: dict[tuple[int, int, str], set[str]] = {}
        self.root_scope = self._build(root, source)
        self.call_nodes.sort(key=lambda call: call.start_byte)
        self.call_starts = [call.start_byte for call in self.call_nodes]
        self._resolve_factory_bindings(source)
        self._resolve_class_factory_fields(source)
        self.mounted_plugin_nodes: set[tuple[int, int, str]] = self._find_mounted_plugins(source)

    @staticmethod
    def node_key(node: Node) -> tuple[int, int, str]:
        return node.start_byte, node.end_byte, node.type

    def calls_in(self, node: Node) -> list[Node]:
        start = bisect_left(self.call_starts, node.start_byte)
        end = bisect_left(self.call_starts, node.end_byte)
        return [
            call
            for call in self.call_nodes[start:end]
            if call.end_byte <= node.end_byte
        ]

    def _find_mounted_plugins(self, source: bytes) -> set[tuple[int, int, str]]:
        """Find named plugin declarations from register calls collected during the first scan."""
        mounted: set[tuple[int, int, str]] = set()
        for current in self.register_calls:
            dispatch = _dispatch_call(current, source, self)
            if dispatch is not None and dispatch[0] == "register" and dispatch[2]:
                plugin_node, _ = _unwrap_plugin(dispatch[2][0], source)
                if plugin_node.type == "identifier":
                    callable_node = self.resolve_callable(
                        node_text(plugin_node, source),
                        plugin_node,
                    )
                    if callable_node is not None:
                        mounted.add(self.node_key(callable_node))
        return mounted

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

            if node.type == "call_expression":
                self.call_nodes.append(node)
                callee = node.child_by_field_name("function")
                if callee is not None and callee.type == "member_expression":
                    prop = callee.child_by_field_name("property")
                    if prop is not None and node_text(prop, source) == "register":
                        self.register_calls.append(node)

            if node.type in _FUNCTION_NODE_TYPES:
                self._index_function(node, scope, source, nested_function)

            if node.type == "variable_declarator":
                self._index_variable(node, scope, source)

            if node.type in {"public_field_definition", "property_definition"}:
                name_node = node.child_by_field_name("name")
                value_node = node.child_by_field_name("value")
                class_node = _enclosing_class(node)
                if (
                    name_node is not None
                    and value_node is not None
                    and value_node.type == "call_expression"
                    and class_node is not None
                ):
                    self._class_factory_fields.append(
                        (self.node_key(class_node), node_text(name_node, source), value_node)
                    )

            if node.type == "assignment_expression":
                left = node.child_by_field_name("left")
                right = node.child_by_field_name("right")
                class_node = _enclosing_class(node)
                if (
                    left is not None
                    and left.type == "member_expression"
                    and right is not None
                    and right.type == "call_expression"
                    and class_node is not None
                ):
                    receiver = left.child_by_field_name("object")
                    name_node = left.child_by_field_name("property")
                    if (
                        receiver is not None
                        and receiver.type == "this"
                        and name_node is not None
                    ):
                        self._class_factory_fields.append(
                            (self.node_key(class_node), node_text(name_node, source), right)
                        )

                if (
                    left is not None
                    and left.type == "identifier"
                    and right is not None
                    and right.type == "call_expression"
                ):
                    self._mark_reassigned_fastify_binding(
                        node_text(left, source),
                        right,
                        source,
                        node,
                    )

            if node.type == "import_statement":
                factory_names = _imported_fastify_factory_names(node, source)
                for name in _imported_binding_names(node, source):
                    scope.add_binding(name, 0, is_fastify_factory=name in factory_names)

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
            string_value=(_string_literal(value_node, source) if value_node is not None else None),
            callable_node=(
                value_node
                if value_node is not None
                and value_node.type in {"arrow_function", "function_expression", "function"}
                else None
            ),
            is_fastify_factory=(
                value_node is not None and _is_fastify_require_call(value_node, source)
            ),
            factory_call=value_node if value_node is not None and value_node.type == "call_expression" else None,
        )

    def _mark_reassigned_fastify_binding(
        self,
        name: str,
        value_node: Node,
        source: bytes,
        node: Node,
    ) -> None:
        if not _is_fastify_factory_call(value_node, source, self):
            return

        scope: Optional[_Scope] = self.scope_for(node)
        while scope is not None:
            if name in scope.bindings:
                binding = scope.binding_at(name, node.start_byte)
                if binding is not None:
                    binding.is_fastify = True
                    binding.factory_call = value_node
                    binding.string_value = None
                    binding.callable_node = None
                return
            scope = scope.parent

    def _resolve_factory_bindings(self, source: bytes) -> None:
        for scope in self.scopes.values():
            for entries in scope.bindings.values():
                for binding in entries:
                    if binding.factory_call is not None:
                        binding.is_fastify = _is_fastify_factory_call(
                            binding.factory_call,
                            source,
                            self,
                        )

    def _resolve_class_factory_fields(self, source: bytes) -> None:
        for class_key, name, factory_call in self._class_factory_fields:
            if _is_fastify_factory_call(factory_call, source, self):
                self.fastify_class_fields.setdefault(class_key, set()).add(name)

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
        current: Optional[Node] = node
        while current is not None:
            key = self.node_key(current)
            if key in self.node_scopes:
                return self.node_scopes[key]
            if current.type in _LEXICAL_SCOPE_NODE_TYPES and key in self.scopes:
                return self.scopes[key]
            current = current.parent
        return self.root_scope

    def is_fastify_instance(self, name: str, node: Node) -> bool:
        scope: Optional[_Scope] = self.scope_for(node)
        while scope is not None:
            if name in scope.bindings:
                binding = scope.binding_at(name, node.start_byte)
                return binding.is_fastify if binding is not None else False
            scope = scope.parent
        return False

    def is_fastify_this_property(self, name: str, node: Node) -> bool:
        class_node = _enclosing_class(node)
        return (
            class_node is not None
            and name in self.fastify_class_fields.get(self.node_key(class_node), set())
        )

    def is_fastify_factory(self, name: str, node: Node) -> Optional[bool]:
        scope: Optional[_Scope] = self.scope_for(node)
        while scope is not None:
            if name in scope.bindings:
                binding = scope.binding_at(name, node.start_byte)
                return binding.is_fastify_factory if binding is not None else False
            scope = scope.parent
        return None

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
    """Enrich captured statements with Fastify routes and return unmatched routes.

    Detects shorthand routes, `route()` configurations, and `register()`
    mounts, including inherited prefixes and resolvable variable values.
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

    statements_by_span: dict[tuple[str, int], list[Statement]] = {}
    for statement in record.statements:
        statements_by_span.setdefault((statement.id, statement.endLine), []).append(statement)

    enclosing = Enclosing(record)
    unmatched: list[Statement] = []
    enriched_ids: set[str] = set()
    span_positions: dict[tuple[str, int], int] = {}
    for route in statements:
        if route.parentId == fid:
            route.parentId = enclosing.function_id(route.startLine, fid)

        source_id = _source_statement_id(route.id)
        span = (source_id, route.endLine)
        candidates = statements_by_span.get(span, ())
        position = span_positions.get(span, 0)
        existing = None
        if position == 0 and position < len(candidates):
            line_statement = enclosing.statement(route.startLine)
            if line_statement is not None and line_statement.id == candidates[position].id:
                existing = line_statement
        if existing is None and position < len(candidates):
            existing = candidates[position]
        while existing is not None and existing.id in enriched_ids:
            position += 1
            existing = candidates[position] if position < len(candidates) else None
        span_positions[span] = position + 1 if existing is not None else position
        if existing is None:
            unmatched.append(route)
            continue

        existing.parentId = route.parentId
        existing.semanticType = route.semanticType
        existing.method = route.method
        existing.endpoint = route.endpoint
        existing.routeKind = route.routeKind
        existing.framework = route.framework
        existing.handler = route.handler
        existing.handlerLine = route.handlerLine
        existing.isRegex = route.isRegex
        existing.authRequired = route.authRequired
        existing.guards = route.guards
        enriched_ids.add(existing.id)

    return unmatched


def _source_statement_id(route_id: str) -> str:
    """Remove the collision suffix added when a route overlays its captured statement."""
    base, separator, suffix = route_id.rpartition("#")
    return base if separator and suffix.isdecimal() else route_id


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
    file_index: _FileIndex,
) -> bool:
    """Check whether a value is created by a Fastify factory call.

    Supports:

        Fastify()
        fastify()
        fastify.default()
        require("fastify")()
        require("fastify").default()
    """
    if value_node.type != "call_expression":
        return False

    callee = value_node.child_by_field_name("function")

    if callee is None:
        return False

    if callee.type == "member_expression":
        prop = callee.child_by_field_name("property")
        if prop is None or node_text(prop, source) != "default":
            return False
        callee = callee.child_by_field_name("object")
        if callee is None:
            return False

    if callee.type == "identifier":
        name = node_text(callee, source)
        imported_factory = file_index.is_fastify_factory(name, value_node)
        return imported_factory is True

    if callee.type == "call_expression":
        return _is_fastify_require_call(callee, source)

    return False


def _is_fastify_require_call(node: Node, source: bytes) -> bool:
    if node.type != "call_expression":
        return False

    callee = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if callee is None or node_text(callee, source) != "require" or arguments is None:
        return False

    argument_nodes = list(arguments.named_children)
    return bool(argument_nodes and _string_literal(argument_nodes[0], source) == "fastify")


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
    """Detect routes from call nodes collected while building the scope index."""
    mounted_plugin_nodes = file_index.mounted_plugin_nodes

    def visit_calls(
        root: Node,
        current_prefix: Optional[str],
        chain: frozenset[tuple[int, int, str]],
        allowed_plugin: Optional[tuple[int, int, str]] = None,
    ) -> None:
        candidates = file_index.calls_in(root)
        # Candidates and suppressed_starts are start-ordered; suppressed_max_ends stores
        # the matching prefix maximum, so bisect selects the latest possible suppressor.
        suppressed_starts: list[int] = []
        suppressed_max_ends: list[int] = []

        for current in candidates:
            previous = bisect_left(suppressed_starts, current.start_byte) - 1
            if previous >= 0 and current.end_byte < suppressed_max_ends[previous]:
                continue

            ancestor = current.parent
            executable = True
            while ancestor is not None and ancestor != root:
                key = file_index.node_key(ancestor)
                if ancestor.type in _NON_EXECUTABLE_NODE_TYPES or (
                    key in mounted_plugin_nodes and key != allowed_plugin
                ):
                    executable = False
                    break
                ancestor = ancestor.parent
            if not executable:
                continue

            dispatch = _dispatch_call(current, source, file_index)
            if dispatch is None:
                continue

            kind, member_name, args = dispatch
            callee = current.child_by_field_name("function")
            chained = callee.child_by_field_name("object") if callee is not None else None

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
                suppressed_starts.append(current.start_byte)
                suppressed_max_ends.append(
                    max(suppressed_max_ends[-1] if suppressed_max_ends else 0, current.end_byte)
                )
                if chained is not None and chained.type == "call_expression":
                    visit_calls(chained, current_prefix, chain, allowed_plugin)
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
                suppressed_starts.append(current.start_byte)
                suppressed_max_ends.append(
                    max(suppressed_max_ends[-1] if suppressed_max_ends else 0, current.end_byte)
                )
                if chained is not None and chained.type == "call_expression":
                    visit_calls(chained, current_prefix, chain, allowed_plugin)
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
                suppressed_starts.append(current.start_byte)
                suppressed_max_ends.append(
                    max(suppressed_max_ends[-1] if suppressed_max_ends else 0, current.end_byte)
                )
                for child, child_prefix in children:
                    child_key = file_index.node_key(child)
                    if child_key not in chain:
                        visit_calls(
                            child,
                            child_prefix,
                            chain | {child_key},
                            child_key,
                        )
                if chained is not None and chained.type == "call_expression":
                    visit_calls(chained, current_prefix, chain, allowed_plugin)

    visit_calls(node, prefix, frozenset())


def _chain_base(node: Optional[Node], source: bytes) -> Optional[Node]:
    """Follow chained Fastify calls (`app.register(a).get(...)`) to the instance."""
    while node is not None and node.type == "call_expression":
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            break
        prop = callee.child_by_field_name("property")
        if prop is None or node_text(prop, source) not in _FASTIFY_CALL_METHODS:
            break
        node = callee.child_by_field_name("object")
    return node


def _enclosing_class(node: Node) -> Optional[Node]:
    current = node.parent
    while current is not None:
        if current.type in {"class_declaration", "class"}:
            return current
        current = current.parent
    return None


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

    if member_name not in _FASTIFY_CALL_METHODS:
        return None

    receiver = _chain_base(callee.child_by_field_name("object"), source)
    if receiver is None:
        return None

    if receiver.type == "identifier":
        receiver_name = node_text(receiver, source)
        is_fastify = file_index.is_fastify_instance(receiver_name, node)
    elif receiver.type == "member_expression":
        owner = receiver.child_by_field_name("object")
        property_node = receiver.child_by_field_name("property")
        is_fastify = (
            owner is not None
            and owner.type == "this"
            and property_node is not None
            and file_index.is_fastify_this_property(node_text(property_node, source), node)
        )
    else:
        return None

    if not is_fastify:
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
    source: bytes,
) -> Optional[Node]:
    """Find the most likely route handler.

    Checks arguments from right to left because the handler is normally
    the last argument, including a handler in a route-options object.
    """
    for candidate in reversed(trailing_args):
        if candidate.type in _CALLABLE_NODE_TYPES:
            return candidate
        if candidate.type == "object":
            handler = _object_pairs(candidate, source).get("handler")
            if handler is not None and (
                handler.type in _CALLABLE_NODE_TYPES or handler.type in _SHORTHAND_PROPERTY_TYPES
            ):
                return handler

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

    regex_path = args[0].type == "regex"
    url = None if regex_path else _resolve_route_string(args[0], source, file_index)

    if not regex_path and url is not None and not (
        url.startswith("/") or url in ("", "*")
    ):
        return

    handler_node = _find_handler_arg(args[1:], source)

    if handler_node is None:
        logger.debug(
            "fastify: no callable handler argument found for %s %s at %s:%d; handler is unresolved",
            member_name.upper(),
            url if url is not None else node_text(args[0], source),
            path,
            node.start_point[0] + 1,
        )

    methods = list(_ALL_METHODS) if member_name == "all" else [member_name.upper()]

    route = _join_prefix(prefix, url) if url is not None else None

    parent_id, handler_name, handler_line = _resolve_handler(
        handler_node,
        source,
        fid=fid,
        fn_by_name=fn_by_name,
    )
    guards = _route_guards(args[1:], source)

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
                guards=guards,
                is_regex=regex_path,
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

    regex_path = url_node.type == "regex"
    url = None if regex_path else _resolve_route_string(url_node, source, file_index)

    if not regex_path and url is not None and not (
        url.startswith("/") or url in ("", "*")
    ):
        return

    display_url = url if url is not None else node_text(url_node, source)
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
            display_url,
            path,
            node.start_point[0] + 1,
        )
        methods.append(None)

    if not methods:
        return

    route = _join_prefix(prefix, url) if url is not None else None

    parent_id, handler_name, handler_line = _resolve_handler(
        handler_node,
        source,
        fid=fid,
        fn_by_name=fn_by_name,
    )
    guards = _route_guards([args[0]], source)

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
                guards=guards,
                is_regex=regex_path,
            )
        )


_AUTH_GUARD_HINTS = (
    "auth",
    "authorize",
    "authenticate",
    "authenticated",
    "login",
    "jwt",
    "oauth",
    "session",
)


def _is_auth_guard_name(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _AUTH_GUARD_HINTS)


def _route_guards(option_nodes: list[Node], source: bytes) -> Optional[list[str]]:
    guards: list[str] = []
    for option_node in option_nodes:
        if option_node.type != "object":
            continue
        pairs = _object_pairs(option_node, source)
        for hook in _ROUTE_HOOKS:
            value = pairs.get(hook)
            if value is None:
                continue
            values = value.named_children if value.type == "array" else [value]
            guards.extend(
                node_text(candidate, source)
                for candidate in values
                if candidate.type in {"identifier", "member_expression"}
            )
    return guards or None


def _route_auth_required(guards: Optional[list[str]]) -> Optional[bool]:
    if not guards:
        return None
    return any(_is_auth_guard_name(name) for name in guards) or None


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

    plugin_node, wrapped = _unwrap_plugin(args[0], source)
    opts_node = args[1] if len(args) > 1 else None

    child_prefix = prefix
    mount_endpoint = (prefix or "/") if prefix is not None else None

    if not wrapped and opts_node is not None and opts_node.type == "object":
        pairs = _object_pairs(opts_node, source)
        prefix_node = pairs.get("prefix")

        if prefix_node is not None:
            declared = _resolve_route_string(prefix_node, source, file_index)

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


def _unwrap_plugin(node: Node, source: bytes) -> tuple[Node, bool]:
    """Strip plugin wrappers and report whether prefix options must be ignored."""
    wrapped = False
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
        wrapped = True
    return node, wrapped


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

    if node.type == "identifier" or node.type in _SHORTHAND_PROPERTY_TYPES:
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


def _run(dc: DetectContext) -> str | None:
    routes = detect_fastify_routes(
        dc.root,
        dc.source,
        dc.path,
        dc.record,
        seen_ids={statement.id for statement in dc.record.statements},
        resolution_index=dc.ctx.resolution_index,
    )
    dc.record.statements.extend(routes)
    found = bool(routes) or any(
        statement.framework == "fastify" for statement in dc.record.statements
    )
    return "fastify" if found else None


# The detector has two byte markers, `fastify` and `Fastify`, so it keeps its own guard.
register_detector(
    Detector(
        name="fastify",
        language="typescript",
        order=15,
        run=_run,
        guard=None,
        skip_fixtures=True,
        frameworks=("fastify",),
    )
)


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
    guards: Optional[list[str]] = None,
    is_regex: bool = False,
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
        isRegex=is_regex,
        authRequired=_route_auth_required(guards),
        guards=guards,
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

    Supports plain strings, static template literals, local variables, and
    values supplied by the shared resolution index.
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


def _resolve_route_string(
    node: Node,
    source: bytes,
    file_index: _FileIndex,
) -> Optional[str]:
    """Resolve route paths, rendering dynamic template substitutions as placeholders."""
    if node.type == "template_string":
        return render_template_url(
            node,
            source,
            resolve_substitution=lambda expression: _resolve_static_string(
                expression, source, file_index
            ),
        )
    return _resolve_static_string(node, source, file_index)


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
