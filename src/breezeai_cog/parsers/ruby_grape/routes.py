"""Conservative Grape route detection over the Ruby AST."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...emit import file_id
from ...schemas import FileRecord
from ..emits import Emit
from ..ruby.ownership import owner_id
from ..treesitter import node_text

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_ROUTE_PREFIXES = {"namespace", "resource", "resources", "route_param"}


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


#: ``version … using:`` strategies that keep the version out of the path.
_NON_PATH_VERSIONING = {"header", "param", "accept_version_header"}


def _version_path(stmt: Node, source: bytes) -> str | None:
    """What a ``version`` call adds to the path: the version with the ``:path`` strategy (Grape's
    default), ``""`` with a header / param strategy, None when it cannot be resolved (a
    non-literal value, several versions, an unknown strategy)."""
    args = _arguments(stmt)
    using = "path"
    for a in args:
        key = a.child_by_field_name("key") if a.type == "pair" else None
        if key is not None and node_text(key, source).rstrip(":") == "using":
            value = a.child_by_field_name("value")
            using = node_text(value, source).lstrip(":").strip("'\"") if value is not None else ""
    if using in _NON_PATH_VERSIONING:
        return ""
    if using != "path":
        return None
    positional = [a for a in args if a.type != "pair"]
    return _literal_path(positional[0], source) if len(positional) == 1 else None


def _api_settings(call: Node, source: bytes) -> str | None:
    """The path Grape's ``prefix`` and ``version`` settings put in front of a route
    (``/prefix/version``, ``""`` for none), or None when one is set but cannot be resolved.

    ``prefix`` is read from the API class body; ``version`` is the last ``version`` call before
    the route in the class body or an enclosing block."""
    prefix = ""
    version: str | None = ""
    version_at = -1
    node = call.parent
    while node is not None:
        if node.type == "body_statement":
            in_class_body = node.parent is not None and _is_grape_api_class(node.parent, source)
            for stmt in node.named_children:
                if stmt.type != "call" or stmt.child_by_field_name("block") is not None:
                    continue
                name = _method(stmt, source)
                if name == "prefix" and in_class_body:
                    args = _arguments(stmt)
                    value = _literal_path(args[0], source) if args else None
                    if value is None:
                        return None
                    prefix = value
                elif name == "version" and version_at < stmt.start_byte < call.start_byte:
                    version_at = stmt.start_byte
                    version = _version_path(stmt, source)
        if _is_grape_api_class(node, source):
            break
        node = node.parent
    if version is None:
        return None
    parts = [p.strip("/") for p in (prefix, version) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else ""


def _before_name(stmt: Node, source: bytes) -> str | None:
    """The guard a ``before { authenticate! }`` / ``before do … end`` hook names: its block's only
    statement when that is a receiver-less method call. Anything else (several statements, inline
    logic) has no single name → None (no fabricated guard)."""
    block = stmt.child_by_field_name("block")
    body = next((c for c in block.named_children if c.type in ("block_body", "body_statement")),
                None) if block is not None else None
    stmts = list(body.named_children) if body is not None else []
    if len(stmts) != 1:
        return None
    only = stmts[0]
    if only.type == "identifier":
        return node_text(only, source)
    return _method(only, source) if only.type == "call" else None


def _guards(call: Node, source: bytes) -> list[str]:
    """``before`` hooks in scope for a route: each enclosing API class body / namespace block that
    declares a ``before`` ahead of the route, outermost scope first. Grape callbacks apply to the
    endpoints defined after them in the same namespace and its children."""
    levels: list[list[str]] = []
    node = call.parent
    while node is not None:
        if node.type in ("body_statement", "block_body"):
            names = [
                name for stmt in node.named_children
                if stmt.type == "call" and stmt.start_byte < call.start_byte
                and _method(stmt, source) == "before"
                and stmt.child_by_field_name("block") is not None
                and (name := _before_name(stmt, source)) is not None
            ]
            levels.append(names)
        if _is_grape_api_class(node, source):
            break
        node = node.parent
    return [name for names in reversed(levels) for name in names]


def _join_path(prefix: str, local: str | None) -> str | None:
    if local is None:
        return None
    parts = [part.strip("/") for part in (prefix, local) if part and part.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def detect_grape_routes(root: Node, source: bytes, path: str, record: FileRecord) -> list[Emit]:
    """The Grape routes whose class and path are syntactically verifiable, for
    :func:`~..emits.apply`. The address is prefix / version (path strategy) / namespaces / path;
    an unresolved part → empty endpoint."""
    fallback = file_id(path)
    found: list[Emit] = []

    for call in _calls(root):
        method = _method(call, source)
        if method not in _HTTP_METHODS or not _has_block(call) or not _in_grape_api(call, source):
            continue
        args = _arguments(call)
        local = _literal_path(args[0], source) if args else ""
        if local is None:
            continue
        settings = _api_settings(call, source)
        prefix = _prefix_for(call, source)
        # An unresolved prefix, version or namespace leaves the address unknown: the route is kept
        # with an empty endpoint, never recorded at a path missing that part.
        endpoint = (
            _join_path(settings + prefix, local) if settings is not None and prefix is not None else None
        )
        line = call.start_point[0] + 1
        block = call.child_by_field_name("block")  # the handler: an anonymous block
        guards = _guards(call, source)
        found.append(
            Emit(call, dict(
                semanticType="route",
                method=method.upper(),
                endpoint=endpoint,
                framework="grape",
                handlerLine=block.start_point[0] + 1 if block is not None else None,
                guards=guards or None,
                # A ``before`` hook runs ahead of every route in its scope — auth, a header, a
                # locale: a scope-level guard never asserts auth (``compose_guards``), so
                # authRequired stays unknown.
                authRequired=None,
                routeKind="route",
                isRegex=False,
            ), parent=owner_id(line, record.functions, record.classes, fallback), node_type="call")
        )
    return found
