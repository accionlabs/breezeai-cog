"""Conservative Grape route detection over the Ruby AST."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..ruby.ownership import owner_id
from ..treesitter import first_line, node_text

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_ROUTE_PREFIXES = {"namespace", "resource", "resources", "route_param", "prefix"}
_GRAPE_API_SUPERCLASSES = {"Grape::API", "Grape::Endpoint"}


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


def _static_string(node: Node, source: bytes) -> str | None:
    """Text of a string whose every part is literal; ``None`` if any part is interpolated.

    A ``"/r#{i}/:id"`` string has ``string_content``, ``interpolation``, ``string_content``
    children — reading only the first content node would emit ``/r`` as if it were the whole
    path. A value that is not fully present in the syntax stays unresolved (honest-null)."""
    parts = node.named_children
    if any(part.type not in {"string_content", "escape_sequence"} for part in parts):
        return None
    return "".join(node_text(part, source) for part in parts)


def _literal_path(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return _static_string(node, source)
    if node.type in {"simple_symbol", "symbol"}:
        value = node_text(node, source)
        return "/" + value.lstrip(":/")
    return None


def _literal_value(node: Node | None, source: bytes) -> str | None:
    """Read a static string or symbol without path normalization."""
    if node is None:
        return None
    if node.type == "string":
        return _static_string(node, source)
    if node.type in {"simple_symbol", "symbol"}:
        return node_text(node, source).lstrip(":")
    return None


def _is_grape_api_class(node: Node, source: bytes) -> bool:
    if node.type != "class":
        return False
    superclass = node.child_by_field_name("superclass")
    if superclass is None:
        return False
    name = node_text(superclass, source).lstrip("<").strip()
    return name in _GRAPE_API_SUPERCLASSES


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
    api_class: Node | None = None
    while current is not None:
        if api_class is None and _is_grape_api_class(current, source):
            api_class = current
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
    class_prefix = _class_prefix_for(api_class, source, before_byte=call.start_byte)
    if class_prefix is None and api_class is not None:
        return None
    nested = list(reversed(prefixes))
    if class_prefix and class_prefix not in nested:
        ordered = [class_prefix, *nested]
    else:
        ordered = nested
    return "/" + "/".join(part.strip("/") for part in ordered if part and part.strip("/")) if ordered else ""


def _class_prefix_for(
    api_class: Node | None, source: bytes, before_byte: int | None = None
) -> str | None:
    class_prefix = ""
    if api_class is not None:
        body = api_class.child_by_field_name("body")
        if body is not None:
            for child in body.named_children:
                if (
                    child.type == "call" and _method(child, source) == "prefix"
                    and (before_byte is None or child.start_byte < before_byte)
                ):
                    args = _arguments(child)
                    class_prefix = _literal_path(args[0], source) if args else None
                    if class_prefix is None:
                        return None
    return class_prefix


def _version_for(call: Node, source: bytes) -> tuple[str | None, bool, bool]:
    """Return (literal version, path strategy, unresolved declaration found)."""
    declarations: list[Node] = []
    enclosing_versions: list[Node] = []
    current = call.parent
    api_class: Node | None = None
    while current is not None:
        if api_class is None and _is_grape_api_class(current, source):
            api_class = current
        if current.type == "call" and _method(current, source) == "version":
            enclosing_versions.append(current)
        current = current.parent
    declarations.extend(enclosing_versions)
    if api_class is not None and not enclosing_versions:
        body = api_class.child_by_field_name("body")
        if body is not None:
            declarations.extend(
                child for child in body.named_children
                if child.type == "call" and _method(child, source) == "version"
                and child.start_byte < call.start_byte
            )
    unique: list[Node] = []
    seen: set[tuple[int, int]] = set()
    for declaration in declarations:
        key = (declaration.start_byte, declaration.end_byte)
        if key not in seen:
            unique.append(declaration)
            seen.add(key)
    if not unique:
        return None, False, False
    declaration = (
        min(enclosing_versions, key=lambda node: node.end_byte - node.start_byte)
        if enclosing_versions else max(unique, key=lambda node: node.start_byte)
    )
    args = _arguments(declaration)
    version = _literal_value(args[0], source) if args else None
    using_path = False
    for arg in args[1:]:
        if arg.type != "pair":
            continue
        key = arg.child_by_field_name("key")
        value = arg.child_by_field_name("value")
        if key is not None and node_text(key, source).rstrip(":") == "using":
            using_path = _literal_value(value, source) == "path"
    return version, using_path, version is None


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
        version, path_version, unresolved_version = _version_for(call, source)
        if unresolved_version and path_version:
            continue
        if path_version:
            api_class = next(
                (ancestor for ancestor in _ancestors(call) if _is_grape_api_class(ancestor, source)),
                None,
            )
            class_prefix = _class_prefix_for(api_class, source, before_byte=call.start_byte) or ""
            class_prefix_path = "/" + class_prefix.strip("/") if class_prefix.strip("/") else ""
            tail = prefix[len(class_prefix_path):] if class_prefix_path else prefix
            versioned_prefix = _join_path(_join_path(class_prefix, version), tail)
            endpoint = _join_path(versioned_prefix, local)
            if local == "/" and not tail and endpoint is not None and not endpoint.endswith("/"):
                endpoint += "/"
        line = call.start_point[0] + 1
        record.statements.append(
            Statement(
                id=disambiguate(statement_id(path, line, call.start_point[1]), seen),
                parentId=owner_id(line, record.functions, record.classes, fallback),
                nodeType="call",
                semanticType="route",
                text=first_line(node_text(call, source)),
                method=method.upper(),
                endpoint=endpoint,
                version=version,
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


def _ancestors(node: Node) -> Iterator[Node]:
    current = node.parent
    while current is not None:
        yield current
        current = current.parent
