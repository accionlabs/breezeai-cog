"""Conservative Rails route detection over the Ruby AST."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..ruby.controllers import controller_class, guards_for
from ..ruby.index import RubyIndex
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
    content = next((child for child in node.named_children if child.type == "string_content"), None)
    return node_text(content, source) if content is not None else ""


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


def _hash_rocket(argument: Node | None, source: bytes) -> tuple[Node | None, Node | None] | None:
    """``'/login' => 'sessions#create'`` (a ``pair`` written with ``=>``) → (path node, target
    node); None for any other argument (a keyword pair like ``to:`` / ``via:`` uses ``:``)."""
    if argument is None or argument.type != "pair":
        return None
    if not any(c.type == "=>" for c in argument.children):
        return None
    return argument.child_by_field_name("key"), argument.child_by_field_name("value")


def _option(arguments: list[Node], source: bytes, name: str) -> Node | None:
    """The value of a keyword option (``via: …``) among the call's arguments, else None."""
    for argument in arguments:
        if argument.type == "pair" and _hash_rocket(argument, source) is None:
            key = argument.child_by_field_name("key")
            if key is not None and node_text(key, source).rstrip(":") == name:
                return argument.child_by_field_name("value")
    return None


def _via_methods(arguments: list[Node], source: bytes) -> list[str | None]:
    """The HTTP methods a ``match`` route answers: ``via: :get`` / ``via: [:get, :post]`` → one
    per verb. No ``via:``, ``via: :all`` or a non-literal value → ``[None]`` (one record, method
    unknown — never a guessed verb)."""
    value = _option(arguments, source, "via")
    items = list(value.named_children) if value is not None and value.type == "array" else [value]
    verbs = [node_text(v, source).lstrip(":").upper() if v is not None and v.type == "simple_symbol"
             else None for v in items]
    if not verbs or None in verbs or "ALL" in verbs:
        return [None]
    return list(dict.fromkeys(verbs))


def _handler(arguments: list[Node], source: bytes) -> str | None:
    """The ``to: 'controller#action'`` target. The action lives in the controller's file, so
    the route has no ``handlerLine`` (the line of the ``to:`` string is not the handler)."""
    for argument in arguments[1:]:
        if argument.type == "pair":
            key = argument.child_by_field_name("key")
            value = argument.child_by_field_name("value")
            if key is not None and node_text(key, source).rstrip(":") == "to":
                if value is not None and value.type == "string":
                    return _string_value(value, source)
                return None
    return None


#: Blocks that add a path segment we compose (``namespace :admin`` → ``/admin``, ``scope '/v1'``).
_PATH_SCOPES = {"namespace", "scope"}
#: Blocks that add a path we do not compute (resource nesting, member/collection, concerns mounted
#: elsewhere): a route inside one has a prefix we cannot resolve → its endpoint stays empty.
_UNRESOLVED_SCOPES = {"resources", "resource", "member", "collection", "concern", "nested", "shallow"}


def _segment(node: Node | None, source: bytes) -> str | None:
    """A path segment literal: ``'v1'`` / ``'/v1'`` / ``:admin`` → its text, else None."""
    if node is None:
        return None
    if node.type == "string":
        return _string_value(node, source)
    if node.type == "simple_symbol":
        return node_text(node, source).lstrip(":")
    return None


def _scope_segment(call: Node, source: bytes) -> tuple[bool, str]:
    """(resolved, segment) a ``namespace`` / ``scope`` block adds to its routes' path.

    ``namespace :admin`` → ``admin`` (``path:`` overrides it); ``scope '/v1'`` / ``scope path: '/v1'``
    → ``/v1``; ``scope module: 'x'`` (no path) → ``""``. A path given by anything but a literal
    → unresolved."""
    args = _arguments(call)
    for a in args:
        if a.type == "pair":
            key = a.child_by_field_name("key")
            if key is not None and node_text(key, source).rstrip(":") == "path":
                seg = _segment(a.child_by_field_name("value"), source)
                return (seg is not None, seg or "")
    positional = [a for a in args if a.type != "pair"]
    if not positional:
        return True, ""  # namespace with no name is not valid Rails; scope module:/as: adds no path
    seg = _segment(positional[0], source)
    return (seg is not None, seg or "")


def _route_prefix(call: Node, source: bytes) -> str | None:
    """The path the enclosing ``namespace`` / ``scope`` blocks put in front of a route (outermost
    first), ``""`` for none, or None when an enclosing block adds a path we cannot resolve."""
    segments: list[str] = []
    current = call.parent
    while current is not None:
        if current.type == "call" and current.child_by_field_name("block") is not None:
            method = _method(current, source)
            if method in _UNRESOLVED_SCOPES:
                return None
            if method in _PATH_SCOPES:
                resolved, seg = _scope_segment(current, source)
                if not resolved:
                    return None
                segments.append(seg)
        current = current.parent
    return "".join("/" + s.strip("/") for s in reversed(segments) if s.strip("/"))


def _route_module(call: Node, source: bytes) -> str | None:
    """The controller module the enclosing blocks put a route in: ``namespace :admin`` → ``admin``
    (``module:`` overrides it), ``scope module: 'v1'`` → ``v1``; ``""`` for none, None when a
    module is given by anything but a literal."""
    modules: list[str] = []
    current = call.parent
    while current is not None:
        if current.type == "call" and current.child_by_field_name("block") is not None:
            method = _method(current, source)
            if method in _PATH_SCOPES:
                args = _arguments(current)
                module: str | None = None
                explicit = False
                for a in args:
                    key = a.child_by_field_name("key") if a.type == "pair" else None
                    if key is not None and node_text(key, source).rstrip(":") == "module":
                        explicit = True
                        module = _segment(a.child_by_field_name("value"), source)
                if explicit:
                    if module is None:
                        return None
                    modules.append(module)
                elif method == "namespace":
                    positional = [a for a in args if a.type != "pair"]
                    name = _segment(positional[0], source) if positional else None
                    if name is None:
                        return None
                    modules.append(name)
        current = current.parent
    return "/".join(m.strip("/") for m in reversed(modules) if m.strip("/"))


def _route_guards(handler: str | None, module: str | None, index: object) -> list[str]:
    """The ``before_action`` guards of the controller action a route points at (``users#show`` →
    ``UsersController#show``, inside ``namespace :admin`` → ``Admin::UsersController``). Unknown
    controller, module or handler form → no guards."""
    if handler is None or module is None or not isinstance(index, RubyIndex):
        return []
    target = controller_class(f"{module}/{handler}" if module else handler)
    if target is None:
        return []
    controller, action = target
    callbacks = index.controller_callbacks.get(controller)
    return guards_for(callbacks, action) if callbacks is not None else []


def _join(prefix: str, local: str) -> str:
    """``/admin`` + ``stats`` → ``/admin/stats``; a path without a leading ``/`` is relative to
    its scope, so it is normalised (``about`` → ``/about``)."""
    parts = [p.strip("/") for p in (prefix, local) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def detect_rails_routes(
    root: Node, source: bytes, path: str, record: FileRecord, index: object = None
) -> bool:
    """Append explicit Rails route DSL statements; unresolved values stay null. ``index`` (the
    Ruby repo index) supplies the controller callbacks that become each route's guards."""
    seen = {statement.id for statement in record.statements}
    matched = False
    fallback = file_id(path)

    for call in _calls(root):
        method = _method(call, source)
        if method not in _HTTP_METHODS or not _inside_routes_draw(call, source):
            continue
        args = _arguments(call)
        rocket = _hash_rocket(args[0], source) if args else None
        if rocket is not None:  # post '/login' => 'sessions#create'
            path_node, target = rocket
            local = _string_value(path_node, source)
            handler = _string_value(target, source)
        else:
            local = _string_value(args[0], source) if args else None
            handler = _handler(args, source)
        prefix = _route_prefix(call, source)
        # A literal path under literal scopes → the full path; an unresolved path or scope prefix
        # leaves the address unknown → empty (never a path missing its prefix).
        endpoint = _join(prefix, local) if local is not None and prefix is not None else None
        line = call.start_point[0] + 1
        methods = _via_methods(args, source) if method == "match" else [method.upper()]
        guards = _route_guards(handler, _route_module(call, source), index)
        for verb in methods:  # match … via: [:get, :post] → one binding per verb
            record.statements.append(
                Statement(
                    id=disambiguate(statement_id(path, line, call.start_point[1]), seen),
                    parentId=owner_id(line, record.functions, record.classes, fallback),
                    nodeType="call",
                    semanticType="route",
                    text=first_line(node_text(call, source)),
                    method=verb,
                    endpoint=endpoint,
                    handler=handler,
                    guards=guards or None,
                    # Honest-null: assert auth only when a guard is present; absence is unknown.
                    authRequired=True if guards else None,
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
