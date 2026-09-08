"""Conservative Grape route detection over the Ruby AST."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..treesitter import first_line, node_text

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_ROUTE_PREFIXES = {"namespace", "resource", "resources", "route_param"}


def _calls(root: Node):
    if root.type == "call":
        yield root
    for child in root.named_children:
        yield from _calls(child)


def _method(call: Node, source: bytes) -> str | None:
    node = call.child_by_field_name("method")
    return node_text(node, source) if node is not None and node.type == "identifier" else None


def _arguments(call: Node) -> list[Node]:
    arguments = call.child_by_field_name("arguments")
    return list(arguments.named_children) if arguments is not None else []


def _literal_path(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        content = next((child for child in node.named_children if child.type == "string_content"), None)
        return node_text(content, source) if content is not None else ""
    if node.type in {"simple_symbol", "symbol"}:
        value = node_text(node, source)
        return "/" + value.lstrip(":/")
    return None


def _is_grape_api_class(node: Node, source: bytes) -> bool:
    if node.type != "class":
        return False
    superclass = node.child_by_field_name("superclass")
    return superclass is not None and "Grape::API" in node_text(superclass, source)


def _in_grape_api(call: Node, source: bytes) -> bool:
    current = call.parent
    while current is not None:
        if _is_grape_api_class(current, source):
            return True
        current = current.parent
    return False


def _has_block(call: Node) -> bool:
    block = call.child_by_field_name("block")
    return block is not None and block.type == "do_block"


def _prefix_for(call: Node, source: bytes) -> str | None:
    prefixes: list[str] = []
    current = call.parent
    while current is not None:
        if current.type == "call" and _method(current, source) in _ROUTE_PREFIXES:
            prefix_method = _method(current, source)
            args = _arguments(current)
            prefix = _literal_path(args[0], source) if args else None
            if prefix is None:
                return None
            if prefix_method == "route_param" and args[0].type in {"simple_symbol", "symbol"}:
                prefix = ":" + prefix.lstrip("/")
            prefixes.append(prefix)
        current = current.parent
    if not prefixes:
        return ""
    return "/" + "/".join(part.strip("/") for part in reversed(prefixes) if part.strip("/"))


def _join_path(prefix: str, local: str | None) -> str | None:
    if local is None:
        return None
    parts = [part.strip("/") for part in (prefix, local) if part and part.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def detect_grape_routes(root: Node, source: bytes, path: str, record: FileRecord) -> bool:
    """Append Grape routes whose class and path are syntactically verifiable."""
    seen = {statement.id for statement in record.statements}
    fallback = file_id(path)
    matched = False

    for call in _calls(root):
        method = _method(call, source)
        if method not in _HTTP_METHODS or not _has_block(call) or not _in_grape_api(call, source):
            continue
        args = _arguments(call)
        local = _literal_path(args[0], source) if args else ""
        prefix = _prefix_for(call, source)
        if prefix is None:
            continue
        endpoint = _join_path(prefix, local)
        if endpoint is None:
            continue
        line = call.start_point[0] + 1
        record.statements.append(
            Statement(
                id=disambiguate(statement_id(path, line, call.start_point[1]), seen),
                parentId=fallback,
                nodeType="call",
                semanticType="route",
                text=first_line(node_text(call, source)),
                method=method.upper(),
                endpoint=endpoint,
                framework="grape",
                routeKind="route",
                isRegex=False,
                startLine=line,
                endLine=call.end_point[0] + 1,
                path=path,
            )
        )
        matched = True
    return matched
