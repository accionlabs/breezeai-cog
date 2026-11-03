"""Conservative Grape route detection over the Ruby AST."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterator

from tree_sitter import Node

from ...emit import file_id
from ...schemas import FileRecord
from ..emits import Emit
from ..ruby.ownership import owner_id
from ..treesitter import node_text

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_ROUTE_PREFIXES = {"namespace", "resource", "resources", "route_param", "prefix"}
_GRAPE_API_SUPERCLASSES = {"Grape::API", "Grape::Endpoint"}
_Version = str | tuple[str, ...] | None
_VersionPath = str | tuple[str | None, ...] | None
_VersionDeclaration = tuple[int, _Version, bool, _VersionPath, bool]
_ScopeBody = tuple[
    str | None,
    bool,
    list[_VersionDeclaration],
    list[_VersionDeclaration],
    list[tuple[int, str]],
]


class _ScopeSummaries:
    """Compact declarations indexed once for bodies encountered during one file parse."""

    def __init__(self, source: bytes) -> None:
        self.source = source
        self._bodies: dict[tuple[int, int], _ScopeBody] = {}

    def body(self, body: Node) -> _ScopeBody:
        key = (body.start_byte, body.end_byte)
        cached = self._bodies.get(key)
        if cached is not None:
            return cached
        prefix: str | None = ""
        prefix_invalid = False
        versions: list[_VersionDeclaration] = []
        setting_versions: list[_VersionDeclaration] = []
        guards: list[tuple[int, str]] = []
        in_class_body = body.parent is not None and _is_grape_api_class(body.parent, self.source)
        for stmt in body.named_children:
            if stmt.type != "call":
                continue
            name = _method(stmt, self.source)
            has_block = stmt.child_by_field_name("block") is not None
            if name == "prefix" and in_class_body and not has_block:
                args = _arguments(stmt)
                prefix = _literal_path(args[0], self.source) if args else None
                prefix_invalid = prefix_invalid or prefix is None
            if name == "version":
                args = _arguments(stmt)
                version = _literal_version_values(args[0], self.source) if args else None
                using_path = True
                for arg in args[1:]:
                    if arg.type != "pair":
                        continue
                    key_node = arg.child_by_field_name("key")
                    value = arg.child_by_field_name("value")
                    if key_node is not None and node_text(key_node, self.source).rstrip(":") == "using":
                        using_path = _literal_value(value, self.source) == "path"
                path = _version_path(stmt, self.source)
                declaration = (stmt.start_byte, version, using_path, path, has_block)
                versions.append(declaration)
                if not has_block:
                    setting_versions.append(declaration)
            if name == "before" and stmt.child_by_field_name("block") is not None:
                guard = _before_name(stmt, self.source)
                if guard is not None:
                    guards.append((stmt.start_byte, guard))
        versions.sort(key=lambda item: item[0])
        setting_versions.sort(key=lambda item: item[0])
        guards.sort(key=lambda item: item[0])
        result = (prefix, prefix_invalid, versions, setting_versions, guards)
        self._bodies[key] = result
        return result

    def latest_preceding_version(
        self, body: Node, position: int
    ) -> _VersionDeclaration | None:
        _prefix, _invalid, versions, _settings, _guards = self.body(body)
        upto = bisect_left(versions, (position,))
        return versions[upto - 1] if upto else None

    def preceding_guards(self, body: Node, position: int) -> list[str]:
        _prefix, _invalid, _versions, _settings, guards = self.body(body)
        upto = bisect_left(guards, (position, ""))
        return [name for _pos, name in guards[:upto]]




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


def _contains_interpolation(node: Node) -> bool:
    return node.type == "interpolation" or any(
        _contains_interpolation(child) for child in node.named_children
    )


def _literal_path(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return _static_string(node, source)
    if node.type in {"simple_symbol", "symbol", "bare_symbol"}:
        if _contains_interpolation(node):
            return None
        value = node_text(node, source)
        return "/" + value.lstrip(":/")
    if node.type == "bare_string":
        return node_text(node, source)
    return None


def _literal_value(node: Node | None, source: bytes) -> str | None:
    """Read a static string or symbol without path normalization."""
    if node is None:
        return None
    if node.type in {"string", "bare_string"}:
        if node.type == "bare_string":
            return node_text(node, source)
        return _static_string(node, source)
    if node.type in {"simple_symbol", "symbol", "bare_symbol"}:
        if _contains_interpolation(node):
            return None
        return node_text(node, source).lstrip(":")
    return None


def _literal_version_values(node: Node | None, source: bytes) -> _Version:
    if node is None:
        return None
    if node.type in {"array", "symbol_array", "string_array"}:
        values = tuple(_literal_value(child, source) for child in node.named_children)
        if not values or any(value is None for value in values):
            return None
        return tuple(value for value in values if value is not None)
    return _literal_value(node, source)


def _is_grape_api_class(node: Node, source: bytes) -> bool:
    if node.type == "class":
        superclass = node.child_by_field_name("superclass")
        if superclass is None:
            return False
        name = node_text(superclass, source).lstrip("<").strip()
        return name in _GRAPE_API_SUPERCLASSES
    if node.type == "call":
        method = node.child_by_field_name("method")
        receiver = node.child_by_field_name("receiver")
        args = _arguments(node)
        return (
            method is not None and node_text(method, source) == "new"
            and receiver is not None and node_text(receiver, source) == "Class"
            and bool(args) and node_text(args[0], source).lstrip(":") in _GRAPE_API_SUPERCLASSES
        )
    return False


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
    ordered = list(reversed(prefixes))
    return "/" + "/".join(part.strip("/") for part in ordered if part and part.strip("/")) if ordered else ""


def _version_for(
    call: Node, source: bytes, summaries: _ScopeSummaries | None = None
) -> tuple[_Version, bool, bool]:
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
            if summaries is None:
                summaries = _ScopeSummaries(source)
            latest = summaries.latest_preceding_version(body, call.start_byte)
            if latest is not None:
                _position, version, using_path, _path, _has_block = latest
                return version, using_path, version is None
    unique: list[Node] = []
    seen: set[tuple[int, int]] = set()
    for declaration in declarations:
        declaration_key = (declaration.start_byte, declaration.end_byte)
        if declaration_key not in seen:
            unique.append(declaration)
            seen.add(declaration_key)
    if not unique:
        return None, False, False
    declaration = (
        min(enclosing_versions, key=lambda node: node.end_byte - node.start_byte)
        if enclosing_versions else max(unique, key=lambda node: node.start_byte)
    )
    args = _arguments(declaration)
    version = _literal_version_values(args[0], source) if args else None
    using_path = True
    for arg in args[1:]:
        if arg.type != "pair":
            continue
        key = arg.child_by_field_name("key")
        value = arg.child_by_field_name("value")
        if key is not None and node_text(key, source).rstrip(":") == "using":
            using_path = _literal_value(value, source) == "path"
    return version, using_path, version is None


#: ``version … using:`` strategies that keep the version out of the path.
_NON_PATH_VERSIONING = {"header", "param", "accept_version_header"}


def _version_path(stmt: Node, source: bytes) -> _VersionPath:
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
    if len(positional) != 1:
        return None
    value = positional[0]
    if value.type in {"array", "symbol_array", "string_array"}:
        paths = tuple(_literal_path(item, source) for item in value.named_children)
        return paths if paths else None
    return _literal_path(value, source)


def _api_settings(
    call: Node,
    source: bytes,
    summaries: _ScopeSummaries | None = None,
    version_index: int = 0,
) -> str | None:
    """The path Grape's ``prefix`` and ``version`` settings put in front of a route
    (``/prefix/version``, ``""`` for none), or None when one is set but cannot be resolved.

    ``prefix`` is read from the API class body; ``version`` is the last ``version`` call before
    the route in the class body or an enclosing block."""
    prefix: str | None = ""
    version_at = -1
    version_path: str | None = ""
    summaries = summaries or _ScopeSummaries(source)
    node = call.parent
    while node is not None:
        if node.type == "body_statement":
            in_class_body = node.parent is not None and _is_grape_api_class(node.parent, source)
            body_prefix, prefix_invalid, _all_versions, body_versions, _guards = summaries.body(node)
            if in_class_body:
                prefix = body_prefix
                if prefix_invalid:
                    return None
            upto = bisect_left(body_versions, (call.start_byte,))
            if upto:
                latest = body_versions[upto - 1]
                if latest[0] > version_at:
                    version_at = latest[0]
                    candidate = latest[3]
                    if isinstance(candidate, tuple):
                        version_path = candidate[version_index] if version_index < len(candidate) else None
                    else:
                        version_path = candidate
        if _is_grape_api_class(node, source):
            break
        node = node.parent
    if version_path is None:
        return None
    parts = [p.strip("/") for p in (prefix, version_path) if p and p.strip("/")]
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


def _guards(
    call: Node, source: bytes, summaries: _ScopeSummaries | None = None
) -> list[str]:
    """``before`` hooks in scope for a route: each enclosing API class body / namespace block that
    declares a ``before`` ahead of the route, outermost scope first. Grape callbacks apply to the
    endpoints defined after them in the same namespace and its children."""
    levels: list[list[str]] = []
    summaries = summaries or _ScopeSummaries(source)
    node = call.parent
    while node is not None:
        if node.type in ("body_statement", "block_body"):
            levels.append(summaries.preceding_guards(node, call.start_byte))
        if _is_grape_api_class(node, source):
            break
        node = node.parent
    return [name for names in reversed(levels) for name in names]


def _join_path(prefix: str, local: str | None) -> str | None:
    if local is None:
        return None
    parts = [part.strip("/") for part in (prefix, local) if part and part.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _route_paths(nodes: list[Node], source: bytes) -> list[str | None]:
    paths: list[str | None] = []
    for node in nodes:
        if node.type in {"pair", "hash"}:
            continue
        items = list(node.named_children) if node.type in {"array", "string_array"} else [node]
        paths.extend(_literal_path(item, source) for item in items)
    return paths or [""]


def _route_verbs(method: str, args: list[Node], source: bytes) -> tuple[list[str], int]:
    if method != "route":
        return [method.upper()], 0
    if not args:
        return [], 0
    value = args[0]
    items = list(value.named_children) if value.type in {"array", "symbol_array"} else [value]
    verbs: list[str] = []
    for item in items:
        raw = _literal_value(item, source)
        if raw is None or raw.lower() == "any" or raw.lower() not in _HTTP_METHODS:
            return [], 1
        verbs.append(raw.upper())
    return list(dict.fromkeys(verbs)), 1


def _mount_prefixes(root: Node, source: bytes) -> dict[str, list[str | None]]:
    mounts: dict[str, list[str | None]] = {}
    for call in _calls(root):
        if _method(call, source) != "mount":
            continue
        args = _arguments(call)
        if not args:
            continue
        child: Node
        prefix: str | None
        first = args[0]
        if first.type == "pair" and any(part.type == "=>" for part in first.children):
            child_node = first.child_by_field_name("key")
            prefix_node = first.child_by_field_name("value")
            if child_node is None:
                continue
            child = child_node
            prefix = _literal_path(prefix_node, source)
        else:
            child, prefix = first, None
        values = mounts.setdefault(node_text(child, source).lstrip(":"), [])
        if prefix not in values:
            values.append(prefix)
    return mounts


def _api_class_name(call: Node, source: bytes) -> str | None:
    current = call.parent
    while current is not None:
        if current.type == "class" and _is_grape_api_class(current, source):
            name = current.child_by_field_name("name")
            return node_text(name, source) if name is not None else None
        current = current.parent
    return None


def detect_grape_routes(root: Node, source: bytes, path: str, record: FileRecord) -> list[Emit]:
    """The Grape routes whose class and path are syntactically verifiable, for
    :func:`~..emits.apply`. The address is prefix / version (path strategy) / namespaces / path;
    an unresolved part → empty endpoint."""
    fallback = file_id(path)
    found: list[Emit] = []
    summaries = _ScopeSummaries(source)
    emitted_calls: set[tuple[int, int]] = set()
    mounts = _mount_prefixes(root, source)

    for call in _calls(root):
        method = _method(call, source)
        if (method not in _HTTP_METHODS and method != "route") \
                or not _has_block(call) or not _in_grape_api(call, source):
            continue
        args = _arguments(call)
        verbs, offset = _route_verbs(method, args, source)
        if not verbs:
            continue
        paths = _route_paths(args[offset:], source)
        versions, path_version, unresolved_version = _version_for(call, source, summaries)
        prefix = _prefix_for(call, source)
        # An unresolved prefix, version or namespace leaves the address unknown: the route is kept
        # with an empty endpoint, never recorded at a path missing that part.
        line = call.start_point[0] + 1
        block = call.child_by_field_name("block")  # the handler: an anonymous block
        guards = _guards(call, source, summaries)
        key = (call.start_byte, call.end_byte)
        api_name = _api_class_name(call, source)
        mount_prefixes = mounts.get(api_name, []) if api_name is not None else []
        if mount_prefixes and None in mount_prefixes:
            known = [value for value in mount_prefixes if value is not None]
            mount_prefixes = known if known else [None]
        if not mount_prefixes:
            mount_prefixes = [""]
        version_values = versions if isinstance(versions, tuple) else (versions,)
        if not version_values:
            version_values = (None,)
        for verb in verbs:
            for local in paths:
                for mount_prefix in mount_prefixes:
                    for version_index, version in enumerate(version_values):
                        settings = _api_settings(call, source, summaries, version_index)
                        unresolved_path_version = unresolved_version and path_version
                        endpoint = (
                            _join_path(settings + prefix, local)
                            if settings is not None and prefix is not None and not unresolved_path_version
                            else None
                        )
                        if endpoint is not None and mount_prefix is None:
                            endpoint = None
                        elif endpoint is not None and mount_prefix:
                            endpoint = _join_path(mount_prefix, endpoint)
                        if local == "/" and not prefix and endpoint and not endpoint.endswith("/"):
                            endpoint += "/"
                        found.append(
                            Emit(
                                call,
                                {
                                    "semanticType": "route",
                                    "method": verb,
                                    "endpoint": endpoint,
                                    "version": version,
                                    "framework": "grape",
                                    "handlerLine": block.start_point[0] + 1
                                    if block is not None
                                    else None,
                                    "guards": guards or None,
                                    "authRequired": None,
                                    "routeKind": "route",
                                    "isRegex": False,
                                },
                                enrich="same" if key not in emitted_calls else "never",
                                parent=owner_id(line, record.functions, record.classes, fallback),
                                node_type="call",
                            )
                        )
                        emitted_calls.add(key)
    return found
