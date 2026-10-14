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


def _method(call: Node, source: bytes) -> str | None:
    if call.child_by_field_name("receiver") is not None:
        return None
    node = call.child_by_field_name("method")
    return node_text(node, source) if node is not None and node.type == "identifier" else None


def _arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name("arguments")
    return list(arguments.named_children) if arguments is not None else []


def _string_value(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type != "string":
        return None
    content = next((child for child in node.named_children if child.type == "string_content"), None)
    return node_text(content, source) if content is not None else ""


def _has_route_block(call: Node) -> bool:
    block = call.child_by_field_name("block")
    return block is not None and block.type == "do_block"


def _namespace_prefix(call: Node, source: bytes) -> str | None:
    """The path enclosing sinatra-contrib ``namespace '/api' do … end`` blocks put in front of a
    route (outermost first; nested namespaces compose), ``""`` for none, or None when a
    namespace's path is not a string literal (a regex or variable) — the address is unknown."""
    segments: list[str] = []
    current = call.parent
    while current is not None:
        if (current.type == "call" and _method(current, source) == "namespace"
                and current.child_by_field_name("block") is not None):
            args = _arguments(current)
            seg = _string_value(args[0], source) if args else None
            if seg is None:
                return None
            segments.append(seg)
        current = current.parent
    return "".join("/" + s.strip("/") for s in reversed(segments) if s.strip("/"))


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
                if prefix is None:
                    continue
                pattern = _pattern_re(prefix + literal)
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
        if method not in _HTTP_METHODS or not _has_route_block(call):
            continue
        args = _arguments(call)
        local = _string_value(args[0], source) if args else None
        if local is None:
            continue
        prefix = _namespace_prefix(call, source)
        # Under an unresolved namespace the address is unknown → empty, never the bare path.
        endpoint = None if prefix is None else (prefix + local if prefix else local)
        line = call.start_point[0] + 1
        block = call.child_by_field_name("block")  # the handler: an anonymous block
        # Global guards don't assert auth (middleware is often logging or sessions).
        guards = _guards_for(call, endpoint, hooks) or None
        found.append(
            Emit(call, dict(
                semanticType="route",
                method=method.upper(),
                endpoint=endpoint,
                framework="sinatra",
                handlerLine=block.start_point[0] + 1 if block is not None else None,
                guards=guards,
                routeKind="route",
                isRegex=False,
            ), parent=owner_id(line, record.functions, record.classes, fallback), node_type="call")
        )
    return found
