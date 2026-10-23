"""Conservative Sinatra route detection over the Ruby AST."""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from ...emit import file_id
from ...schemas import FileRecord
from ..emits import Emit
from ..ruby.ownership import owner_id
from ..treesitter import node_text

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


def _namespace_prefix(call: Node, source: bytes) -> tuple[str | None, bool] | None:
    """Return the statically known namespace prefix and whether one exists."""
    scopes: list[str] = []
    current = call.parent
    while current is not None:
        if (
            current.type == "call"
            and _method(current, source) == "namespace"
            and current.child_by_field_name("block") is not None
        ):
            args = _arguments(current)
            prefix = _string_value(args[0], source) if args else None
            if prefix is None:
                return None, True
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


def _before_name(call: Node, source: bytes) -> str | None:
    """The guard a ``before do … end`` filter names: its block's only statement when that is a
    receiver-less method call (``authenticate!``). Anything else has no single name → None."""
    block = call.child_by_field_name("block")
    body = next((c for c in block.named_children if c.type in ("block_body", "body_statement")),
                None) if block is not None else None
    stmts = list(body.named_children) if body is not None else []
    if len(stmts) != 1:
        return None
    only = stmts[0]
    if only.type == "identifier":
        return node_text(only, source)
    return _method(only, source) if only.type == "call" else None


def _is_app_class(node: Node, source: bytes) -> bool:
    if node.type != "class":
        return False
    sup = node.child_by_field_name("superclass")
    return sup is not None and "Sinatra::" in node_text(sup, source)


def _scope(call: Node, source: bytes) -> Node:
    """The node whose routes a filter / middleware covers: the nearest enclosing ``namespace``
    block, ``class App < Sinatra::Base`` body, or the file (classic style)."""
    node = call.parent
    while node is not None:
        if node.type == "call" and _method(node, source) == "namespace":
            block = node.child_by_field_name("block")
            if block is not None:
                return block
        if _is_app_class(node, source):
            return node
        if node.parent is None:
            return node
        node = node.parent
    return call


def _pattern_re(pattern: str) -> re.Pattern[str]:
    """A Sinatra path pattern (``/admin/*``, ``/users/:id``) as a regex over route paths."""
    parts = re.split(r"(\*|:[A-Za-z_]\w*)", pattern)
    body = "".join(".*" if p == "*" else "[^/]+" if p.startswith(":") else re.escape(p)
                   for p in parts if p)
    return re.compile(body + r"\Z")


def _global_guards(root: Node, source: bytes) -> list[tuple[Node, int, int, str, re.Pattern[str] | None]]:
    """Filters and middleware that run before routes (decision Q9): ``use Middleware`` and
    ``before do … end`` (optionally path-scoped: ``before '/admin/*' do … end``), each with the
    scope it covers. Both apply to every route of their scope whatever the declaration order. A
    path pattern that is not a string literal, or a filter with no single name, adds nothing."""
    out: list[tuple[Node, int, int, str, re.Pattern[str] | None]] = []
    for call in _calls(root):
        method = _method(call, source)
        args = _arguments(call)
        if method == "use" and args and args[0].type in ("constant", "scope_resolution"):
            out.append((_scope(call, source), 0, call.start_byte, node_text(args[0], source), None))
        elif method == "before" and call.child_by_field_name("block") is not None:
            name = _before_name(call, source)
            if name is None:
                continue
            pattern = None
            if args:
                literal = _string_value(args[0], source)
                if literal is None:
                    continue  # a regex or variable path: coverage unknown
                prefix = _namespace_prefix(call, source)
                if prefix is None or prefix[0] is None:
                    continue
                namespace_prefix, _ = prefix
                pattern = _pattern_re(_join_endpoint(namespace_prefix, literal))
            out.append((_scope(call, source), 1, call.start_byte, name, pattern))
    return out


def _guards_for(call: Node, endpoint: str | None,
                hooks: list[tuple[Node, int, int, str, re.Pattern[str] | None]]) -> list[str]:
    """The guards covering a route, outermost scope first, middleware before filters."""
    covering = [
        (-(scope.end_byte - scope.start_byte), kind, pos, name)
        for scope, kind, pos, name, pattern in hooks
        if scope.start_byte <= call.start_byte and call.end_byte <= scope.end_byte
        and (pattern is None or (endpoint is not None and pattern.match(endpoint)))
    ]
    return [name for *_, name in sorted(covering)]


def detect_sinatra_routes(root: Node, source: bytes, path: str, record: FileRecord) -> list[Emit]:
    """The Sinatra routes whose path is an explicit string literal, prefixed by their enclosing
    ``namespace`` blocks, for :func:`~..emits.apply`."""
    fallback = file_id(path)
    found: list[Emit] = []
    hooks = _global_guards(root, source) if b"before" in source or b"use " in source else []

    for call in _calls(root):
        method = _method(call, source)
        if (method not in _HTTP_METHODS and method != "route") or not _has_route_block(call):
            continue
        namespace = _namespace_prefix(call, source)
        if namespace is None:
            continue
        prefix, in_namespace = namespace
        args = _arguments(call)
        combinations = _route_combinations(
            method, args, source, allow_pathless=in_namespace and bool(prefix)
        )
        if not combinations:
            continue
        line = call.start_point[0] + 1
        block = call.child_by_field_name("block")  # the handler: an anonymous block
        for index, (verb, local) in enumerate(combinations):
            endpoint = None if prefix is None else _join_endpoint(prefix, local)
            # Global guards don't assert auth (middleware is often logging or sessions).
            guards = _guards_for(call, endpoint, hooks) or None
            found.append(
                Emit(call, dict(
                    semanticType="route",
                    method=verb,
                    endpoint=endpoint,
                    framework="sinatra",
                    handlerLine=block.start_point[0] + 1 if block is not None else None,
                    guards=guards,
                    routeKind="route",
                    isRegex=False,
                ), enrich="same" if index == 0 else "never",
                    parent=owner_id(line, record.functions, record.classes, fallback),
                    node_type="call")
            )
    return found
