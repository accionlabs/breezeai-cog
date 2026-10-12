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


def detect_sinatra_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Append Sinatra routes whose path is an explicit string literal, prefixed by their enclosing
    ``namespace`` blocks."""
    seen = {statement.id for statement in record.statements}
    fallback = file_id(path)
    matched = False

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
        record.statements.append(
            Statement(
                id=disambiguate(statement_id(path, line, call.start_point[1]), seen),
                parentId=owner_id(line, record.functions, record.classes, fallback),
                nodeType="call",
                semanticType="route",
                text=first_line(node_text(call, source)),
                method=method.upper(),
                endpoint=endpoint,
                framework="sinatra",
                handlerLine=block.start_point[0] + 1 if block is not None else None,
                routeKind="route",
                isRegex=False,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            )
        )
        matched = True
    return matched
