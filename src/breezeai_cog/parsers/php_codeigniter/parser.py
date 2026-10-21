"""CodeIgniter framework parser with CodeIgniter 4 route detection."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..base import ParseContext
from ..php.parser import PhpParser
from ..php_route_common import (
    args_of,
    closure_argument,
    closure_body,
    enrich_route,
    group_filters,
    join_endpoint,
    method_of,
    normalize_endpoint,
    route_handler,
    route_options,
    string_value,
    strings_in_array,
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
    "add": "ANY",
    "cli": "CLI",
    "view": "GET",
}

_CODEIGNITER_DECLARATION = re.compile(
    rb"(?m)^[\t ]*(?:use|namespace)\s+[^;\r\n]*\bCodeIgniter\\"
)
_CODEIGNITER_ROUTE_CALL = re.compile(
    rb"\$[A-Za-z_]\w*\s*->\s*(?:get|post|put|patch|delete|options|head|any|add|match|group|resource|presenter|map|addRedirect|cli|view|environment)\s*\("
)
_ROUTE_FILE = re.compile(rb"(?:^|/)(?:routes?|routing)(?:/|\.php$)", re.IGNORECASE)
_CODEIGNITER_DOC = re.compile(rb"(?:^|/)user_guide_src/", re.IGNORECASE)


def _group_prefix(call: Node, source: bytes) -> str | None:
    args = args_of(call)
    return string_value(args[0], source) if args else None


def _route_methods(
    method: str | None, args: list[Node], source: bytes
) -> tuple[list[str], int, int | None]:
    if method == "match" and len(args) >= 2:
        return [item.upper() for item in strings_in_array(args[0], source)], 1, 2 if len(args) > 2 else None
    if method == "addredirect":
        return ["ANY"], 0, 1 if len(args) > 1 else None
    if method in _VERBS:
        return [_VERBS[method]], 0, 1 if len(args) > 1 else None
    return [], 0, None


def _static_array_map(node: Node | None, source: bytes) -> dict[str, Node]:
    """Return statically keyed PHP array entries without guessing dynamic keys."""
    if node is None:
        return {}
    node = node.named_children[0] if node.type == "argument" and len(node.named_children) == 1 else node
    if node.type != "array_creation_expression":
        return {}
    result: dict[str, Node] = {}
    for element in node.named_children:
        children = list(element.named_children)
        if len(children) < 2:
            continue
        key = string_value(children[0], source)
        if key is not None:
            result[key] = children[1]
    return result


def _route_receiver(receiver: Node | None, source: bytes) -> bool:
    if receiver is None:
        return False
    text = node_text(receiver, source).strip()
    final_name = text.lstrip("$").rsplit("->", 1)[-1].lower()
    if final_name in {"route", "routes", "routecollection"}:
        return True
    return re.fullmatch(r"service\s*\(\s*['\"]routes['\"]\s*\)", text, re.IGNORECASE) is not None


def _resource_specs(
    method: str, args: list[Node], source: bytes
) -> list[tuple[str, str, str, str]]:
    """Expand CodeIgniter's resource/presenter convenience APIs to route facts."""
    if method not in {"resource", "presenter"} or not args:
        return []
    name = string_value(args[0], source)
    if name is None:
        return []
    options = _static_array_map(args[1] if len(args) > 1 else None, source)
    controller = string_value(options.get("controller"), source)
    controller = controller[:1].upper() + controller[1:] if controller else None
    target = controller or "\\".join(part[:1].upper() + part[1:] for part in name.split("/"))
    placeholder = string_value(options.get("placeholder"), source) or "(:segment)"
    placeholder = f"({placeholder.strip('()')})"
    default = (
        ["index", "show", "create", "update", "delete", "new", "edit"]
        if method == "resource"
        else ["index", "show", "new", "create", "edit", "update", "remove", "delete"]
    )
    only = strings_in_array(options.get("only"), source)
    if len(only) == 1 and "," in only[0]:
        only = [item.strip() for item in only[0].split(",") if item.strip()]
    actions = only if "only" in options else default
    excluded = set(strings_in_array(options.get("except"), source))
    if len(excluded) == 1 and "," in next(iter(excluded)):
        excluded = {item.strip() for item in next(iter(excluded)).split(",") if item.strip()}
    actions = [action for action in actions if action not in excluded]

    if method == "resource":
        definitions = {
            "index": [("GET", name, "index")],
            "new": [("GET", f"{name}/new", "new")],
            "edit": [("GET", f"{name}/{placeholder}/edit", "edit")],
            "show": [("GET", f"{name}/{placeholder}", "show")],
            "create": [("POST", name, "create")],
            "update": [("PUT", f"{name}/{placeholder}", "update"), ("PATCH", f"{name}/{placeholder}", "update")],
            "delete": [("DELETE", f"{name}/{placeholder}", "delete")],
        }
        specs = [item for action in actions for item in definitions.get(action, [])]
        if "websafe" in options:
            if "delete" in actions:
                specs.append(("POST", f"{name}/{placeholder}/delete", "delete"))
            if "update" in actions:
                specs.append(("POST", f"{name}/{placeholder}", "update"))
        return [(verb, endpoint, f"{target}@{action}", "resource") for verb, endpoint, action in specs]

    definitions = {
        "index": [("GET", name, "index")],
        "show": [("GET", f"{name}/show/{placeholder}", "show"), ("GET", f"{name}/{placeholder}", "show")],
        "new": [("GET", f"{name}/new", "new")],
        "create": [("POST", f"{name}/create", "create"), ("POST", name, "create")],
        "edit": [("GET", f"{name}/edit/{placeholder}", "edit")],
        "update": [("POST", f"{name}/update/{placeholder}", "update")],
        "remove": [("GET", f"{name}/remove/{placeholder}", "remove")],
        "delete": [("POST", f"{name}/delete/{placeholder}", "delete")],
    }
    return [(verb, endpoint, f"{target}@{action}", "presenter")
            for action in actions for verb, endpoint, action in definitions.get(action, [])]


def _enrich_route(
    call: Node,
    source: bytes,
    path: str,
    statements: dict[str, object],
    all_statements: list,
    seen_ids: set[str],
    *,
    endpoint: str,
    method: str,
    handler_node: Node | None = None,
    handler: str | None = None,
    name: str | None = None,
    guards: list[str] | None = None,
    route_kind: str = "route",
) -> bool:
    from ..php_route_common import enrich_route

    normalized = normalize_endpoint(endpoint)
    line = call.start_point[0] + 1
    base = statements.get(statement_id(path, line, call.start_point[1]))
    if base is not None and base.semanticType == "route" and base.method == method and base.endpoint != normalized:
        rendered_handler, rendered_handler_line = route_handler(handler_node, source)
        clone = base.model_copy(update={
            "id": disambiguate(statement_id(path, line, call.start_point[1]), seen_ids),
            "semanticType": "route",
            "framework": "codeigniter",
            "method": method,
            "endpoint": normalized,
            "handler": handler if handler is not None else rendered_handler,
            "handlerLine": line if handler is not None else rendered_handler_line,
            "name": name,
            "routeKind": route_kind,
            "isRegex": bool(any(token in endpoint for token in ("(:num)", "(:segment)", "(:any)", "(.*)"))),
            "guards": list(dict.fromkeys(guards or [])) or None,
        })
        all_statements.append(Statement.model_validate(clone))
        return True

    changed = enrich_route(
        call, source, path, statements, all_statements, seen_ids,
        framework="codeigniter", endpoint=endpoint, method=method,
        handler_node=handler_node, name=name, guards=guards,
        is_regex=bool(any(token in endpoint for token in ("(:num)", "(:segment)", "(:any)", "(.*)"))),
    )
    for statement in reversed(all_statements):
        if statement.startLine == line and statement.method == method and statement.endpoint == normalized:
            if handler is not None:
                statement.handler = handler
                statement.handlerLine = line
            statement.routeKind = route_kind
            changed = True
            break
    return changed


def _detect_codeigniter_events(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    statements = {statement.id: statement for statement in record.statements}
    found = False
    for node in _walk(root):
        if node.type != "scoped_call_expression":
            continue
        receiver = node.child_by_field_name("scope")
        if receiver is None or not node_text(receiver, source).lstrip("\\").endswith("Events"):
            continue
        method = method_of(node, source)
        if method not in {"on", "trigger"}:
            continue
        args = args_of(node)
        event_name = string_value(args[0], source) if args else None
        statement = statements.get(statement_id(path, node.start_point[0] + 1, node.start_point[1]))
        if statement is None or event_name is None or statement.semanticType is not None:
            continue
        handler, handler_line = route_handler(args[1], source) if method == "on" and len(args) > 1 else (None, None)
        statement.semanticType = "eventbus_consumer" if method == "on" else "eventbus_publish"
        statement.framework = "codeigniter"
        statement.method = "CONSUMER" if method == "on" else "PUBLISH"
        statement.endpoint = event_name
        statement.handler = handler
        statement.handlerLine = handler_line
        found = True
    return found


def _repair_codeigniter_db_hints(record: FileRecord, source: bytes) -> None:
    """Correct shared PHP call heuristics when the surrounding code proves CI semantics."""
    source_text = source.decode("utf-8", "replace")
    is_ci_model = bool(re.search(
        r"(?:namespace\s+CodeIgniter\\|use\s+CodeIgniter\\Model|extends\s+(?:BaseModel|Model)\b|class\s+BaseModel\b)",
        source_text,
    ))
    is_ci_builder = bool(re.search(
        r"(?:namespace\s+CodeIgniter\\Database|use\s+CodeIgniter\\Database|class\s+BaseBuilder\b)",
        source_text,
    ))
    for statement in record.statements:
        if statement.semanticType != "db_method_call":
            continue
        method = (statement.method or "").lower()
        if method == "findall" and statement.dataAccessHint == "sequelize" and is_ci_model:
            statement.dataAccessHint = "codeigniter-builder"
        elif method == "upsert" and statement.dataAccessHint == "typeorm" and is_ci_builder:
            statement.dataAccessHint = "codeigniter-builder"


def _walk(node: Node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def detect_codeigniter_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    statements = {statement.id: statement for statement in record.statements}
    seen_ids = {statement.id for statement in record.statements}
    found = False

    def visit(
        node: Node, prefix: str = "", guards: list[str] | None = None
    ) -> None:
        nonlocal found
        if node.type == "member_call_expression":
            method = method_of(node, source)
            args = args_of(node)
            if method == "group":
                closure = closure_argument(node)
                if closure is not None:
                    group_prefix = _group_prefix(node, source)
                    group_guards = group_filters(args[1] if len(args) > 1 else None, source)
                    visit(
                        closure_body(closure),
                        join_endpoint(prefix, group_prefix or "") if group_prefix else prefix,
                        list(dict.fromkeys([*(guards or []), *group_guards])),
                    )
                    return
            if method == "environment":
                closure = closure_argument(node)
                if closure is not None:
                    visit(closure_body(closure), prefix, guards)
                    return
            if method in {"resource", "presenter"}:
                if _route_receiver(node.child_by_field_name("object"), source):
                    options = args[1] if len(args) > 1 else None
                    route_name, route_guards = route_options(options, source)
                    for verb, endpoint, handler, kind in _resource_specs(method, args, source):
                        found |= _enrich_route(
                            node, source, path, statements, record.statements, seen_ids,
                            endpoint=join_endpoint(prefix, endpoint), method=verb,
                            handler=handler, name=route_name,
                            guards=list(dict.fromkeys([*(guards or []), *route_guards])),
                            route_kind=kind,
                        )
                    return
            if method == "map" and _route_receiver(node.child_by_field_name("object"), source):
                options = args[1] if len(args) > 1 else None
                route_name, route_guards = route_options(options, source)
                for endpoint_node, handler_node in _static_array_map(args[0] if args else None, source).items():
                    handler, _ = route_handler(handler_node, source)
                    found |= _enrich_route(
                        node, source, path, statements, record.statements, seen_ids,
                        endpoint=join_endpoint(prefix, endpoint_node), method="ANY",
                        handler=handler, name=route_name,
                        guards=list(dict.fromkeys([*(guards or []), *route_guards])),
                    )
                return
            methods, path_index, handler_index = _route_methods(method, args, source)
            endpoint = string_value(args[path_index], source) if len(args) > path_index else None
            options_index = handler_index + 1 if handler_index is not None else None
            options = args[options_index] if options_index is not None and len(args) > options_index else None
            route_name, route_guards = route_options(options, source)
            receiver = node.child_by_field_name("object")
            if endpoint is not None and _route_receiver(receiver, source):
                route_kind = "redirect" if method == "addredirect" else "view" if method == "view" else "route"
                for verb in methods:
                    found |= _enrich_route(
                        node, source, path, statements, record.statements, seen_ids,
                        endpoint=join_endpoint(prefix, endpoint), method=verb,
                        handler_node=args[handler_index] if handler_index is not None else None,
                        name=route_name,
                        guards=list(dict.fromkeys([*(guards or []), *route_guards])),
                        route_kind=route_kind,
                    )
        for child in node.named_children:
            visit(child, prefix, guards)

    visit(root)
    return found


class CodeIgniterParser(PhpParser):
    name = "php-codeigniter"
    priority = 20
    frameworks = ["codeigniter"]

    def claims(self, path: str, source: bytes) -> bool:
        if _CODEIGNITER_DECLARATION.search(source):
            return True
        normalized_path = path.replace("\\", "/").encode()
        return bool(
            _CODEIGNITER_DOC.search(normalized_path)
            or (_CODEIGNITER_ROUTE_CALL.search(source) and _ROUTE_FILE.search(normalized_path))
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("php", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)
        record.framework = "codeigniter"
        _repair_codeigniter_db_hints(record, ctx.source)
        if ctx.capture_statements and not self.is_fixture_file(ctx.path):
            detect_codeigniter_routes(root, ctx.source, ctx.path, record)
            _detect_codeigniter_events(root, ctx.source, ctx.path, record)
        return record
