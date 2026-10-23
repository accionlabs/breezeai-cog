"""Conservative Sinatra route detection over the Ruby AST."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..ruby.ownership import owner_id
from ..treesitter import first_line, node_text

_HTTP_METHODS = {
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "link",
    "unlink",
}


def _calls(root: Node) -> Iterator[Node]:
    if root.type == "call":
        yield root
    for child in root.named_children:
        yield from _calls(child)


def _is_app_receiver(call: Node, source: bytes) -> bool:
    receiver = call.child_by_field_name("receiver")
    if receiver is None:
        return True

    receiver_name = node_text(receiver, source)
    if receiver.type == "instance_variable":
        return receiver_name == "@app" or receiver_name.endswith("_app")

    # Sinatra extension callbacks receive the app class as `klass`.
    if receiver.type == "identifier" and receiver_name == "klass":
        parent = call.parent
        while parent is not None:
            if parent.type in {"method", "singleton_method"}:
                name = parent.child_by_field_name("name")
                return name is not None and node_text(name, source) == "registered"
            parent = parent.parent
    return False


def _method(call: Node, source: bytes) -> str | None:
    if not _is_app_receiver(call, source):
        return None
    node = call.child_by_field_name("method")
    return node_text(node, source) if node is not None and node.type == "identifier" else None


def _arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name("arguments")
    return list(arguments.named_children) if arguments is not None else []


def _string_value(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type != "string":
        return None
    parts = node.named_children
    if any(part.type not in {"string_content", "escape_sequence"} for part in parts):
        return None
    return "".join(node_text(part, source) for part in parts)


def _http_method_value(node: Node, source: bytes) -> str | None:
    if node.type == "string":
        value = _string_value(node, source)
    elif node.type == "simple_symbol":
        value = node_text(node, source)[1:]
    else:
        return None
    method = value.lower() if value is not None else ""
    return method.upper() if method in _HTTP_METHODS else None


def _literal_paths(nodes: list[Node], source: bytes) -> list[str]:
    paths: list[str] = []
    for node in nodes:
        if node.type in {"hash", "pair"}:
            break  # route options/conditions follow the path arguments
        path = _string_value(node, source)
        if path is None:
            return []  # do not emit a partial result when another path is dynamic
        paths.append(path)
    return paths


def _route_combinations(
    method: str, args: list[Node], source: bytes, *, allow_pathless: bool = False
) -> list[tuple[str, str]]:
    if method != "route":
        paths = _literal_paths(args, source)
        if not args and allow_pathless:
            paths = [""]
        return [(method.upper(), path) for path in paths]

    methods: list[str] = []
    for node in args:
        value = _http_method_value(node, source)
        if value is None:
            break
        methods.append(value)
    if not methods:
        return []

    paths = _literal_paths(args[len(methods) :], source)
    if len(args) == len(methods) and allow_pathless:
        paths = [""]
    return [(verb, path) for verb in methods for path in paths]


def _namespace_prefix(call: Node, source: bytes) -> tuple[str, bool] | None:
    """Return the statically known composed namespace prefix and whether one exists."""
    scopes: list[str] = []
    current = call.parent
    while current is not None:
        if current.type == "call":
            method = current.child_by_field_name("method")
            if method is not None and node_text(method, source) == "namespace":
                args = _arguments(current)
                prefix = _string_value(args[0], source) if args else None
                if prefix is None:
                    return None
                scopes.append(prefix)
        current = current.parent
    if not scopes:
        return "", False
    prefix = ""
    for part in reversed(scopes):
        if not part:
            continue
        prefix = prefix.rstrip("/") + "/" + part.strip("/")
    return prefix, True


def _join_endpoint(prefix: str, endpoint: str) -> str:
    if not prefix:
        return endpoint
    if not endpoint:
        return prefix
    if endpoint == "/":
        return prefix + "/"
    return prefix.rstrip("/") + "/" + endpoint.lstrip("/")


def _has_route_block(call: Node) -> bool:
    block = call.child_by_field_name("block")
    return block is not None and block.type in {"do_block", "block"}


def detect_sinatra_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Append Sinatra routes whose path is an explicit string literal."""
    seen = {statement.id for statement in record.statements}
    fallback = file_id(path)
    matched = False
    detected_routes: list[Statement] = []

    for call in _calls(root):
        method = _method(call, source)
        if (method not in _HTTP_METHODS and method != "route") or not _has_route_block(call):
            continue
        namespace = _namespace_prefix(call, source)
        if namespace is None:
            continue
        prefix, in_namespace = namespace
        args = _arguments(call)
        combinations = _route_combinations(method, args, source, allow_pathless=in_namespace and bool(prefix))
        if not combinations:
            continue
        combinations = [(verb, _join_endpoint(prefix, endpoint)) for verb, endpoint in combinations]
        line = call.start_point[0] + 1
        base_id = statement_id(path, line, call.start_point[1])
        existing = next(
            (statement for statement in record.statements if statement.id == base_id),
            None,
        )
        for index, (verb, endpoint) in enumerate(combinations):
            if index == 0 and existing is not None:
                existing.semanticType = "route"
                existing.method = verb
                existing.endpoint = endpoint
                existing.framework = "sinatra"
                existing.routeKind = "route"
                existing.isRegex = False
                detected_routes.append(existing)
                continue

            statement = Statement(
                id=disambiguate(base_id, seen),
                parentId=owner_id(line, record.functions, record.classes, fallback),
                nodeType="call",
                semanticType="route",
                text=first_line(node_text(call, source)),
                method=verb,
                endpoint=endpoint,
                framework="sinatra",
                routeKind="route",
                isRegex=False,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            )
            record.statements.append(statement)
            detected_routes.append(statement)
        matched = True

    detected_route_ids = {statement.id for statement in detected_routes}
    if detected_routes:
        record.statements = [
            statement for statement in record.statements if statement.id not in detected_route_ids
        ] + detected_routes
    return matched
