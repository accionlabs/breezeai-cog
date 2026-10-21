"""Conservative Rails route detection over the Ruby AST."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ...emit import file_id
from ...schemas import FileRecord
from ..emits import Emit
from ..ruby.ownership import owner_id
from ..method_lines import locate_method
from ..treesitter import node_text
from .callbacks import callbacks_of, controller_class, guards_for

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
    per verb. ``via: :all`` → ``["ANY"]`` (one handler for every verb). No ``via:`` or a
    non-literal value → ``[None]`` (one record, method unknown — never a guessed verb)."""
    value = _option(arguments, source, "via")
    items = list(value.named_children) if value is not None and value.type == "array" else [value]
    verbs = [node_text(v, source).lstrip(":").upper() if v is not None and v.type == "simple_symbol"
             else None for v in items]
    if "ALL" in verbs:
        return ["ANY"]
    if not verbs or None in verbs:
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
#: Resource blocks: their routes are nested under the resource (``resources :photos do`` →
#: ``/photos/:photo_id/…``; ``member do`` → ``/photos/:id/…``; ``collection do`` → ``/photos/…``).
_RESOURCE_SCOPES = {"resources", "resource"}
_MEMBER_SCOPES = {"member", "collection"}
#: Blocks that add a path we do not compute (concerns mounted elsewhere, shallow nesting): a route
#: inside one has a prefix we cannot resolve → its endpoint stays empty.
_UNRESOLVED_SCOPES = {"concern", "nested", "shallow", "new"}

#: The RESTful actions ``resources`` / ``resource`` declare: (action, verb, path suffix). ``{id}``
#: is the member segment (``:id``, or ``:<param>``); singular resources have no index and no id.
_PLURAL_ACTIONS = (
    ("index", "GET", ""), ("create", "POST", ""), ("new", "GET", "/new"),
    ("edit", "GET", "/{id}/edit"), ("show", "GET", "/{id}"), ("update", "PATCH", "/{id}"),
    ("update", "PUT", "/{id}"), ("destroy", "DELETE", "/{id}"),
)
_SINGULAR_ACTIONS = (
    ("create", "POST", ""), ("new", "GET", "/new"), ("edit", "GET", "/edit"),
    ("show", "GET", ""), ("update", "PATCH", ""), ("update", "PUT", ""),
    ("destroy", "DELETE", ""),
)
#: Irregular nouns Rails inflects (singular ↔ plural) that the suffix rules below get wrong.
_IRREGULAR = {"person": "people", "child": "children", "man": "men", "woman": "women",
              "mouse": "mice", "goose": "geese", "foot": "feet", "tooth": "teeth"}
_IRREGULAR_PLURAL = {v: k for k, v in _IRREGULAR.items()}


def _singular(name: str) -> str | None:
    """ActiveSupport-style singular of a resource name (``photos`` → ``photo``, ``categories`` →
    ``category``, ``people`` → ``person``); None when the word is not one the rules are sure of
    (a guessed parameter name would be a wrong address)."""
    if name in _IRREGULAR_PLURAL:
        return _IRREGULAR_PLURAL[name]
    if name.endswith("ies") and len(name) > 3:
        return name[:-3] + "y"
    if name.endswith(("sses", "shes", "ches", "xes", "zes")):
        return name[:-2]
    if name.endswith("s") and not name.endswith(("ss", "us", "is")):
        return name[:-1]
    return None


def _plural(name: str) -> str | None:
    """ActiveSupport-style plural (``profile`` → ``profiles``): a singular ``resource`` is served
    by the plural controller. None when unsure."""
    if name in _IRREGULAR:
        return _IRREGULAR[name]
    if name.endswith("y") and len(name) > 1 and name[-2] not in "aeiou":
        return name[:-1] + "ies"
    if name.endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s" if name and name[-1].isalpha() else None


class _Resource:
    """One ``resources`` / ``resource`` declaration: the path segment, its controller, the
    member parameter, the actions it declares (None = not known) — or ``ok`` False when a part
    that shapes the address is not a literal."""

    def __init__(self, call: Node, name_node: Node, source: bytes) -> None:
        self.plural = _method(call, source) == "resources"
        args = _arguments(call)
        name = _segment(name_node, source)
        self.ok = name is not None and name_node.type == "simple_symbol"
        self.name = name or ""
        path = _option(args, source, "path")
        seg = _segment(path, source) if path is not None else self.name
        controller = _option(args, source, "controller")
        ctrl = _segment(controller, source) if controller is not None else (
            self.name if self.plural else _plural(self.name))
        param = _option(args, source, "param")
        param_name = _segment(param, source) if param is not None else "id"
        self.ok = self.ok and seg is not None and param_name is not None
        self.segment = (seg or "").strip("/")
        self.controller = ctrl
        self.param = param_name or "id"
        singular = _singular(self.name) if self.plural else self.name
        self.nested_param = f":{singular}_{self.param}" if singular is not None else None
        self.shallow = _option(args, source, "shallow") is not None
        self.actions = self._actions(args, source)

    def _actions(self, args: list[Node], source: bytes) -> set[str] | None:
        """``only:`` / ``except:`` → the declared actions; a non-literal list → None."""
        table = _PLURAL_ACTIONS if self.plural else _SINGULAR_ACTIONS
        every = {a for a, _, _ in table}
        for key, keep in (("only", True), ("except", False)):
            value = _option(args, source, key)
            if value is None:
                continue
            items = list(value.named_children) if value.type == "array" else [value]
            names = {_segment(i, source) for i in items}
            if None in names:
                return None
            chosen = {n for n in names if n}
            return (every & chosen) if keep else (every - chosen)
        return every

    def member(self) -> str:
        return f"/{self.segment}/:{self.param}" if self.plural else f"/{self.segment}"

    def collection(self) -> str:
        return f"/{self.segment}"

    def nested(self) -> str | None:
        if not self.plural:
            return f"/{self.segment}"
        return f"/{self.segment}/{self.nested_param}" if self.nested_param else None


def _resource_of(call: Node, source: bytes) -> _Resource | None:
    """The single resource a ``resources`` / ``resource`` block call declares, or None (several
    names, or none)."""
    names = [a for a in _arguments(call) if a.type != "pair"]
    return _Resource(call, names[0], source) if len(names) == 1 else None


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


def _route_prefix(call: Node, source: bytes, on: str | None = None) -> str | None:
    """The path the enclosing blocks put in front of a route (outermost first), ``""`` for none,
    or None when an enclosing block adds a path we cannot resolve.

    ``namespace`` / ``scope`` add their segment; a ``resources`` block nests its routes —
    under ``/photos/:photo_id`` directly, ``/photos/:id`` in ``member do``, ``/photos`` in
    ``collection do`` (``on:`` gives the same choice for a single route). A singular ``resource``
    nests everything under ``/profile``."""
    segments: list[str] = []
    kind = on  # member / collection / new for the nearest resource, if any
    current = call.parent
    while current is not None:
        if current.type == "call" and current.child_by_field_name("block") is not None:
            method = _method(current, source)
            if method in _UNRESOLVED_SCOPES:
                return None
            if method in _MEMBER_SCOPES:
                if kind is not None:
                    return None  # member inside member — not valid routing
                kind = method
            elif method in _RESOURCE_SCOPES:
                resource = _resource_of(current, source)
                if resource is None or not resource.ok or resource.shallow:
                    return None
                if kind == "member":
                    seg = resource.member()
                elif kind == "collection":
                    seg = resource.collection()
                elif kind is None:
                    nested = resource.nested()
                    if nested is None:
                        return None
                    seg = nested
                else:
                    return None
                segments.append(seg)
                kind = None
            elif method in _PATH_SCOPES:
                resolved, seg = _scope_segment(current, source)
                if not resolved:
                    return None
                segments.append(seg)
        current = current.parent
    if kind is not None:
        return None  # member / collection / on: outside a resource
    return "".join("/" + s.strip("/") for s in reversed(segments) if s.strip("/"))


def _nearest_resource(call: Node, source: bytes) -> _Resource | None:
    """The resource whose block a route is declared in (for its default controller)."""
    current = call.parent
    while current is not None:
        if current.type == "call" and current.child_by_field_name("block") is not None:
            if _method(current, source) in _RESOURCE_SCOPES:
                return _resource_of(current, source)
        current = current.parent
    return None


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
    if handler is None or module is None:
        return []
    target = controller_class(f"{module}/{handler}" if module else handler)
    if target is None:
        return []
    controller, action = target
    callbacks = callbacks_of(index, controller)
    return guards_for(callbacks, action) if callbacks is not None else []


def _join(prefix: str, local: str) -> str:
    """``/admin`` + ``stats`` → ``/admin/stats``; a path without a leading ``/`` is relative to
    its scope, so it is normalised (``about`` → ``/about``)."""
    parts = [p.strip("/") for p in (prefix, local) if p and p.strip("/")]
    return "/" + "/".join(parts) if parts else "/"


def _action_name(local: str | None) -> str | None:
    """``preview`` / ``/preview`` → ``preview``: a route path that is one plain word names its
    action (Rails' default inside a resource); anything else (``a/b``, ``:id``) does not."""
    word = (local or "").strip("/")
    return word if word.isidentifier() else None


def _root_handler(args: list[Node], source: bytes) -> str | None:
    """``root 'home#index'`` / ``root to: 'home#index'`` / ``root :to => 'home#index'``."""
    for a in args:
        if a.type == "string":
            return _string_value(a, source)
        if a.type == "pair":
            key = a.child_by_field_name("key")
            if key is not None and node_text(key, source).strip(":") == "to":
                value = a.child_by_field_name("value")
                return _string_value(value, source) if value is not None else None
    return None


def detect_rails_routes(
    root: Node, source: bytes, path: str, record: FileRecord, index: object = None
) -> list[Emit]:
    """Explicit Rails route DSL bindings, for :func:`~..emits.apply`; unresolved values stay
    null. ``index`` (the Ruby repo index) supplies the controller callbacks
    (``rails.controller-callbacks``) that become each route's guards.

    ``resources :photos`` / ``resource :profile`` expand to one binding per RESTful action
    (``only:`` / ``except:`` honoured) and nest the routes in their block; ``root`` is ``GET`` at
    its scope's path."""
    found: list[Emit] = []
    fallback = file_id(path)

    def emit(call: Node, verb: str | None, endpoint: str | None, handler: str | None) -> None:
        line = call.start_point[0] + 1
        module = _route_module(call, source)
        guards = _route_guards(handler, module, index)
        # `users#show` → the `show` action of `UsersController`, where it is declared (Q14).
        target = controller_class(f"{module}/{handler}" if module else handler) \
            if handler is not None and module is not None else None
        site = locate_method(index, "ruby", *target) if target is not None else None
        handler_line = handler_path = None
        if site is not None and target is not None:
            handler, handler_line = target[1], site[1]
            handler_path = site[0] if site[0] != path else None
        found.append(
            Emit(call, dict(
                semanticType="route",
                method=verb,
                endpoint=endpoint,
                handler=handler,
                handlerLine=handler_line,
                handlerPath=handler_path,
                guards=guards or None,
                # Honest-null: assert auth only when a guard is present; absence is unknown.
                authRequired=True if guards else None,
                framework="rails",
                routeKind="route",
                isRegex=False,
            ), parent=owner_id(line, record.functions, record.classes, fallback), node_type="call")
        )

    handler: str | None
    endpoint: str | None
    for call in _calls(root):
        method = _method(call, source)
        if method is None or not _inside_routes_draw(call, source):
            continue
        args = _arguments(call)
        if method in _RESOURCE_SCOPES:
            prefix = _route_prefix(call, source)
            for name_node in [a for a in args if a.type != "pair"]:
                resource = _Resource(call, name_node, source)
                if resource.actions is None:
                    continue  # only:/except: not a literal list — which actions exist is unknown
                table = _PLURAL_ACTIONS if resource.plural else _SINGULAR_ACTIONS
                base = (prefix + "/" + resource.segment) if prefix is not None and resource.ok \
                    else None
                for action, verb, suffix in table:
                    if action not in resource.actions:
                        continue
                    endpoint = base + suffix.replace("{id}", ":" + resource.param) \
                        if base is not None else None
                    handler = f"{resource.controller}#{action}" if resource.controller else None
                    emit(call, verb, endpoint, handler)
            continue
        if method == "root":
            prefix = _route_prefix(call, source)
            emit(call, "GET", _join(prefix, "") if prefix is not None else None,
                 _root_handler(args, source))
            continue
        if method not in _HTTP_METHODS:
            continue
        rocket = _hash_rocket(args[0], source) if args else None
        if rocket is not None:  # post '/login' => 'sessions#create'
            path_node, target = rocket
            local = _string_value(path_node, source)
            handler = _string_value(target, source)
        else:
            local = _segment(args[0], source) if args and args[0].type != "pair" else None
            handler = _handler(args, source)
        on = _segment(_option(args, source, "on"), source)
        if on is None and _option(args, source, "on") is not None:
            local = None  # on: given by a non-literal → the nesting is unknown
        owner = _nearest_resource(call, source)
        if handler is None and rocket is None and owner is not None and owner.controller:
            named = _action_name(local)  # inside a resource: get :preview → photos#preview
            handler = f"{owner.controller}#{named}" if named else None
        route_prefix = _route_prefix(call, source, on)
        # A literal path under literal scopes → the full path; an unresolved path or scope prefix
        # leaves the address unknown → empty (never a path missing its prefix).
        endpoint = (_join(route_prefix, local)
                    if local is not None and route_prefix is not None else None)
        methods = _via_methods(args, source) if method == "match" else [method.upper()]
        for route_verb in methods:  # match … via: [:get, :post] → one binding per verb
            emit(call, route_verb, endpoint, handler)
    return found
