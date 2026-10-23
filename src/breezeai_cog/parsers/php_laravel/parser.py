"""Laravel framework parser with route, event, and Laravel-specific call detection."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ...schemas.enums import SemanticType
from ..base import ParseContext
from ..php.parser import PhpParser
from ..php_route_common import (
    PHP_CALL_TYPES,
    args_of,
    chained_route_metadata,
    chain_prefix,
    closure_argument,
    closure_body,
    enrich_route,
    join_endpoint,
    method_of,
    receiver_chain_metadata,
    route_handler,
    route_options,
    string_value,
    strings_in_array,
    unwrap,
    walk,
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
}

_SPECIAL_ROUTES = {
    "fallback": "ANY",
    "view": "GET",
    "redirect": "GET",
}

_RESOURCE_ACTIONS = {
    "resource": {
        "index": [("GET", "{base}", "index")],
        "create": [("GET", "{base}/create", "create")],
        "store": [("POST", "{base}", "store")],
        "show": [("GET", "{base}/{{id}}", "show")],
        "edit": [("GET", "{base}/{{id}}/edit", "edit")],
        "update": [("PUT", "{base}/{{id}}", "update"), ("PATCH", "{base}/{{id}}", "update")],
        "destroy": [("DELETE", "{base}/{{id}}", "destroy")],
    },
    "api_resource": {
        "index": [("GET", "{base}", "index")],
        "store": [("POST", "{base}", "store")],
        "show": [("GET", "{base}/{{id}}", "show")],
        "update": [("PUT", "{base}/{{id}}", "update"), ("PATCH", "{base}/{{id}}", "update")],
        "destroy": [("DELETE", "{base}/{{id}}", "destroy")],
    },
    "singleton": {
        "show": [("GET", "{base}", "show")],
        "edit": [("GET", "{base}/edit", "edit")],
        "update": [("PUT", "{base}", "update"), ("PATCH", "{base}", "update")],
        "destroy": [("DELETE", "{base}", "destroy")],
    },
    "api_singleton": {
        "show": [("GET", "{base}", "show")],
        "update": [("PUT", "{base}", "update"), ("PATCH", "{base}", "update")],
        "destroy": [("DELETE", "{base}", "destroy")],
    },
}


def _last_name(value: str) -> str:
    return value.rsplit("\\", 1)[-1].rsplit("->", 1)[-1].lstrip("$").lower()


def _route_scope(call: Node, source: bytes) -> bool:
    current: Node | None = call
    while current is not None:
        scope = current.child_by_field_name("scope")
        if scope is not None and _last_name(node_text(scope, source)) == "route":
            return True
        current = current.child_by_field_name("object")
    return False


def _router_receiver(call: Node, source: bytes) -> bool:
    """Recognize Router/Registrar instances without treating every ``->get`` as a route."""
    receiver = call.child_by_field_name("object")
    if receiver is None:
        return False
    text = node_text(receiver, source)
    terminal = _last_name(text)
    if terminal in {"router", "route", "registrar"}:
        return True
    lower = source.lower()
    return (
        (b"illuminate\\routing\\router" in lower or b"illuminate\\contracts\\routing\\registrar" in lower)
        and terminal in {"this", "self"}
        and "router" in text.lower()
    )


def _is_route_call(call: Node, source: bytes) -> bool:
    return _route_scope(call, source) or _router_receiver(call, source)


def _direct_prefix(call: Node, source: bytes) -> str | None:
    prefix = chain_prefix(call.child_by_field_name("object"), source)
    if prefix is not None:
        return prefix
    args = args_of(call)
    if args:
        direct = string_value(args[0], source)
        if direct is not None:
            return direct
        options = args[0].named_children[0] if args[0].named_children else None
        if options is not None and options.type == "array_creation_expression":
            for pair in options.named_children:
                parts = list(pair.named_children)
                if len(parts) > 1 and node_text(parts[0], source).strip("'\"") == "prefix":
                    return string_value(parts[-1], source)
    return None


def _has_dynamic_prefix(call: Node, source: bytes) -> bool:
    """Whether a group has a prefix argument/chain that is present but unresolved."""
    args = args_of(call)
    if args:
        first = unwrap(args[0])
        if first is not None and first.type in {"anonymous_function", "arrow_function"}:
            first = None
        if first is None:
            args = []
    if args:
        first = args[0]
        if string_value(first, source) is None:
            value = first.named_children[0] if first.named_children else first
            if value.type != "array_creation_expression":
                return True
            for pair in value.named_children:
                parts = list(pair.named_children)
                if len(parts) > 1 and node_text(parts[0], source).strip("'\"") == "prefix":
                    return string_value(parts[-1], source) is None
    object_node = call.child_by_field_name("object")
    return object_node is not None and chain_prefix(object_node, source) is None and any(
        method_of(node, source) == "prefix" for node in walk(object_node)
    )


def _laravel_resource_specs(name: str, controller: str | None, kind: str) -> list[tuple[str, str, str]]:
    actions = _RESOURCE_ACTIONS[kind]
    target = controller or name.rsplit("/", 1)[-1].replace("-", " ").title().replace(" ", "") + "Controller"
    return [
        (method, endpoint.format(base=name), f"{target}@{action}")
        for action in actions
        for method, endpoint, _ in actions[action]
    ]


def detect_laravel_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    statements = {statement.id: statement for statement in record.statements}
    seen_ids = {statement.id for statement in record.statements}
    found = False

    def visit(
        node: Node,
        prefix: str | None = "",
        name_prefix: str = "",
        inherited_guards: list[str] | None = None,
    ) -> None:
        nonlocal found
        if node.type in {"member_call_expression", "scoped_call_expression"}:
            name = method_of(node, source)
            if name == "group" and (node.type != "scoped_call_expression" or _route_scope(node, source)):
                closure = closure_argument(node)
                if closure is not None:
                    next_prefix = _direct_prefix(node, source)
                    group_name, group_guards = receiver_chain_metadata(
                        node,
                        source,
                        name_methods={"name"},
                        guard_methods={"middleware"},
                    )
                    args = args_of(node)
                    option_name, option_guards = route_options(args[0], source) if args else (None, [])
                    visit(
                        closure_body(closure),
                        None if _has_dynamic_prefix(node, source) else (
                            join_endpoint(prefix or "", next_prefix or "") if next_prefix else prefix
                        ),
                        f"{name_prefix}{option_name or ''}{group_name or ''}",
                        list(dict.fromkeys([
                            *(inherited_guards or []), *option_guards, *group_guards,
                        ])),
                    )
                    return
            if _is_route_call(node, source):
                args = args_of(node)
                endpoint_node: Node | None = None
                handler_node: Node | None = None
                if name in {"resource", "apiresource", "singleton", "apisingleton"}:
                    endpoint = string_value(args[0], source) if args else None
                    controller, _ = route_handler(args[1], source) if len(args) > 1 else (None, None)
                    kind = {
                        "resource": "resource",
                        "apiresource": "api_resource",
                        "singleton": "singleton",
                        "apisingleton": "api_singleton",
                    }[name]
                    if endpoint is not None and controller is not None:
                        chain_route_prefix = chain_prefix(node.child_by_field_name("object"), source)
                        effective_prefix = (
                            join_endpoint(prefix or "", chain_route_prefix)
                            if chain_route_prefix and prefix is not None else prefix
                        )
                        for method, binding, handler in _laravel_resource_specs(endpoint, controller, kind):
                            found |= enrich_route(
                                node, source, path, statements, record.statements, seen_ids,
                                framework="laravel",
                                endpoint=(join_endpoint(effective_prefix or "", binding)
                                          if effective_prefix is not None else None),
                                method=method, handler_node=None, handler_name=handler,
                                route_kind="route",
                            )
                    return
                if name == "match" and len(args) >= 2:
                    methods = [m.upper() for m in strings_in_array(args[0], source)]
                    endpoint_node, handler_node = args[1], args[2] if len(args) > 2 else None
                    route_kind = "route"
                elif name in _VERBS and args:
                    methods = [_VERBS[name]]
                    endpoint_node, handler_node = args[0], args[1] if len(args) > 1 else None
                    route_kind = "route"
                elif name in _SPECIAL_ROUTES:
                    method = _SPECIAL_ROUTES[name]
                    route_kind = "route"
                    methods = [method]
                    endpoint_node = args[0] if args else None
                    handler_node = None if name in {"view", "redirect"} else (
                        args[1] if len(args) > 1 else None
                    )
                    if name == "fallback":
                        handler_node = args[0] if args else None
                else:
                    methods = []
                    endpoint_node, handler_node = None, None
                    route_kind = "route"
                endpoint = string_value(endpoint_node, source)
                if methods and (endpoint is not None or name == "fallback"):
                    chain_route_prefix = chain_prefix(node.child_by_field_name("object"), source)
                    effective_prefix = (
                        join_endpoint(prefix or "", chain_route_prefix)
                        if chain_route_prefix and prefix is not None else prefix
                    )
                    for method in methods:
                        route_name, guards = chained_route_metadata(
                            node,
                            source,
                            name_methods={"name"},
                            guard_methods={"middleware"},
                        )
                        if route_name:
                            route_name = f"{name_prefix}{route_name}"
                        found |= enrich_route(
                            node, source, path, statements, record.statements, seen_ids,
                            framework="laravel",
                            endpoint=(join_endpoint(effective_prefix or "", endpoint)
                                      if endpoint is not None and effective_prefix is not None else None),
                            method=method,
                            handler_node=handler_node,
                            name=route_name,
                            guards=list(dict.fromkeys([*(inherited_guards or []), *guards])),
                            route_kind=route_kind,
                        )
        for child in node.named_children:
            visit(child, prefix, name_prefix, inherited_guards)

    visit(root)
    return found


def _call_receiver(call: Node, source: bytes) -> str:
    receiver = call.child_by_field_name("scope") or call.child_by_field_name("object")
    return node_text(receiver, source).lower() if receiver is not None else ""


def _event_endpoint(call: Node, source: bytes, listener: bool = False) -> str | None:
    args = args_of(call)
    if listener:
        return string_value(args[0], source) if args else None
    for arg in args[:2]:
        value = string_value(arg, source)
        if value is not None:
            return value
    return None


def _event_kind(call: Node, source: bytes) -> tuple[SemanticType, str] | None:
    method = method_of(call, source)
    if method is None and call.type == "function_call_expression":
        function = call.child_by_field_name("function")
        method = node_text(function, source).lower() if function is not None else None
    if method is None:
        return None
    receiver = _call_receiver(call, source)
    source_lower = source.lower()
    event_marker = any(marker in source_lower for marker in (
        b"illuminate\\contracts\\events\\dispatcher",
        b"illuminate\\events\\dispatcher",
        b"illuminate\\support\\facades\\event",
        b"illuminate\\support\\facades\\" if b"event::" in source_lower else b"__no_event_facade__",
        b"namespace illuminate\\events",
    ))
    bus_marker = any(marker in source_lower for marker in (
        b"illuminate\\contracts\\bus\\dispatcher",
        b"illuminate\\bus\\dispatcher",
        b"illuminate\\support\\facades\\bus",
        b"illuminate\\support\\facades\\" if b"bus::" in source_lower else b"__no_bus_facade__",
        b"illuminate\\foundation\\bus",
        b"namespace illuminate\\bus",
    ))
    scoped = _last_name(receiver)
    if method in {"listen", "subscribe", "addlistener"} and (
        (scoped in {"event", "events", "dispatcher"} or (
            scoped in {"this", "self"} and b"namespace illuminate\\events" in source_lower
        )) and event_marker
    ):
        return "eventbus_consumer", "CONSUMER"
    if method in {"dispatch", "until", "dispatchafterresponse", "dispatchsync", "dispatchnow", "dispatchtoqueue"}:
        if (
            scoped in {"event", "events", "eventdispatcher"}
            or (scoped in {"this", "self"} and b"namespace illuminate\\events" in source_lower)
        ) and event_marker:
            return "eventbus_publish", "PUBLISH"
        if (
            scoped in {"bus", "dispatcher"}
            or (scoped in {"this", "self"} and b"namespace illuminate\\bus" in source_lower)
        ) and bus_marker:
            return "eventbus_send", "MESSAGE"
    if call.type == "function_call_expression" and method == "dispatch" and bus_marker:
        return "eventbus_send", "MESSAGE"
    if call.type == "function_call_expression" and method == "event" and event_marker:
        return "eventbus_publish", "PUBLISH"
    return None


def detect_laravel_events(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Emit Laravel Event/Bus dispatch and listener semantics with conservative ownership."""
    by_id = {statement.id: statement for statement in record.statements}
    seen_ids = set(by_id)
    found = False
    for call in walk(root):
        if call.type not in PHP_CALL_TYPES:
            continue
        kind = _event_kind(call, source)
        if kind is None:
            continue
        semantic_type, method = kind
        line, column = call.start_point[0] + 1, call.start_point[1]
        sid = statement_id(path, line, column)
        statement = by_id.get(sid)
        if statement is None:
            functions = [
                function for function in record.functions
                if function.startLine <= line <= function.endLine
            ]
            parent_id = max(functions, key=lambda function: function.startLine).id if functions else record.id
            statement = Statement(
                id=disambiguate(sid, seen_ids),
                parentId=parent_id,
                nodeType=call.type,
                text=node_text(call, source),
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            )
            record.statements.append(statement)
            by_id[statement.id] = statement
        if statement.semanticType is not None and statement.semanticType != semantic_type:
            continue
        statement.semanticType = semantic_type
        statement.framework = "laravel"
        statement.method = method
        statement.endpoint = _event_endpoint(
            call, source, listener=semantic_type == "eventbus_consumer"
        )
        found = True
    return found


def _clear_semantic(statement: Statement) -> None:
    statement.semanticType = None
    statement.framework = None
    statement.method = None
    statement.endpoint = None
    statement.dataAccessHint = None


class LaravelParser(PhpParser):
    name = "php-laravel"
    priority = 10
    frameworks = ["laravel"]

    def claims(self, path: str, source: bytes) -> bool:
        lower = source.lower()
        return (
            b"Illuminate\\Support\\Facades\\Route" in source
            or b"Illuminate\\Support\\Facades\\Event" in source
            or b"Illuminate\\Support\\Facades\\Bus" in source
            or (b"Illuminate\\Routing\\" in source and b"Route::" in source)
            or b"namespace illuminate\\" in lower
            or b"use illuminate\\" in lower
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        record.framework = "laravel"
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            detect_laravel_routes(root, ctx.source, ctx.path, record)
            detect_laravel_events(root, ctx.source, ctx.path, record)
        return record
