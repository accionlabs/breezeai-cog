"""Conservative Rails route detection over the Ruby AST."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..ruby.ownership import owner_id
from ..treesitter import first_line, node_text

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "match"}


def _calls(root: Node) -> Iterator[Node]:
    if root.type == "call":
        yield root
    for child in root.named_children:
        yield from _calls(child)


def _method(call: Node, source: bytes) -> str | None:
    node = call.child_by_field_name("method")
    return node_text(node, source) if node is not None and node.type == "identifier" else None


def _string_value(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type != "string":
        return None
    parts = node.named_children
    if any(part.type not in {"string_content", "escape_sequence"} for part in parts):
        return None
    return "".join(node_text(part, source) for part in parts)


def _inside_routes_draw(call: Node, source: bytes) -> bool:
    current = call.parent
    while current is not None:
        if current.type == "call" and _method(current, source) == "draw":
            receiver = current.child_by_field_name("receiver")
            if receiver is not None and node_text(receiver, source) == "Rails.application.routes":
                return True
        current = current.parent
    return False


def _arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name("arguments")
    return list(arguments.named_children) if arguments is not None else []


def _handler(arguments: list[Node], source: bytes) -> tuple[str | None, int | None]:
    for argument in arguments[1:]:
        if argument.type == "pair":
            key = argument.child_by_field_name("key")
            value = argument.child_by_field_name("value")
            if key is not None and node_text(key, source).rstrip(":") == "to":
                if value is not None and value.type == "string":
                    return _string_value(value, source), value.start_point[0] + 1
                return None, None
    return None, None


# Wrappers that never change the URL path, so a route inside them keeps the outer prefix.
_TRANSPARENT_SCOPES = {"constraints", "defaults", "controller"}


def _static_text(node: Node | None, source: bytes) -> str | None:
    """Text of a fully literal string or symbol; ``None`` if anything is interpolated."""
    if node is None:
        return None
    if node.type == "simple_symbol":
        return node_text(node, source).lstrip(":")
    if node.type == "string":
        parts = node.named_children
        if any(part.type not in {"string_content", "escape_sequence"} for part in parts):
            return None
        return "".join(node_text(part, source) for part in parts)
    return None


def _path_option(arguments: list[Node], source: bytes) -> tuple[bool, str | None]:
    """``(present, literal value)`` of a ``path:`` option."""
    for argument in arguments:
        if argument.type != "pair":
            continue
        key = argument.child_by_field_name("key")
        if key is not None and node_text(key, source).rstrip(":") == "path":
            return True, _static_text(argument.child_by_field_name("value"), source)
    return False, None


def _segment(call: Node, method: str, source: bytes) -> str | None:
    """Path segment a ``namespace``/``scope`` block adds; ``None`` when not statically known.

    Mirrors Rails: ``namespace :admin`` and ``scope "/admin"`` add ``/admin``; ``path:``
    overrides the name; ``scope module:/as:/constraints:`` alone add nothing. Anything else
    (``resources``/``member``, a variable, an interpolated string) is unresolved."""
    args = _arguments(call)
    positional = [a for a in args if a.type != "pair"]
    has_path, path_value = _path_option(args, source)
    if method == "namespace":
        if has_path:
            return path_value
        return _static_text(positional[0], source) if len(positional) == 1 else None
    if method == "scope":
        if has_path:
            return path_value if not positional else None
        if not positional:
            return ""
        if len(positional) == 1 and positional[0].type == "string":
            return _static_text(positional[0], source)
        return None
    return None


def _scope_prefix(call: Node, source: bytes) -> str | None:
    """Composed path prefix of the blocks enclosing a route inside ``routes.draw``.

    ``""`` = no prefix; ``None`` = an enclosing block's path cannot be determined statically."""
    segments: list[str] = []
    current = call.parent
    while current is not None:
        if current.type == "call":
            method = _method(current, source)
            receiver = current.child_by_field_name("receiver")
            if method == "draw" and receiver is not None and (
                node_text(receiver, source) == "Rails.application.routes"
            ):
                break
            if method not in _TRANSPARENT_SCOPES:
                segment = _segment(current, method or "", source)
                if segment is None:
                    return None
                segments.append(segment)
        current = current.parent
    parts = [part.strip("/") for part in reversed(segments) if part.strip("/")]
    return "/" + "/".join(parts) if parts else ""


def _join_prefix(prefix: str, endpoint: str | None) -> str | None:
    if not prefix:
        return endpoint
    if endpoint is None:
        return None
    local = endpoint.strip("/")
    return f"{prefix}/{local}" if local else prefix


def detect_rails_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Append explicit Rails route DSL statements; unresolved values stay null."""
    seen = {statement.id for statement in record.statements}
    matched = False
    fallback = file_id(path)

    for call in _calls(root):
        method = _method(call, source)
        if method not in _HTTP_METHODS or not _inside_routes_draw(call, source):
            continue
        args = _arguments(call)
        endpoint = _string_value(args[0], source) if args else None
        prefix = _scope_prefix(call, source)
        # An enclosing block with an unresolved path means the real endpoint is unknown.
        endpoint = None if prefix is None else _join_prefix(prefix, endpoint)
        handler, handler_line = _handler(args, source)
        line = call.start_point[0] + 1
        record.statements.append(
            Statement(
                id=disambiguate(statement_id(path, line, call.start_point[1]), seen),
                parentId=owner_id(line, record.functions, record.classes, fallback),
                nodeType="call",
                semanticType="route",
                text=first_line(node_text(call, source)),
                method=method.upper() if method != "match" else None,
                endpoint=endpoint,
                handler=handler,
                handlerLine=handler_line,
                framework="rails",
                routeKind="route",
                isRegex=False,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            )
        )
        matched = True
    return matched
