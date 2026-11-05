"""Flask blueprint wiring across files, as a registered fact on the Python index: ``flask.wiring``.

A blueprint is usually declared in one file (``bp = Blueprint("auth", __name__)`` in
``app/auth/__init__.py``), given its routes in another (``@bp.route("/login")`` in
``app/auth/routes.py``) and registered in a third (``app.register_blueprint(auth_bp,
url_prefix="/auth")`` in ``app/__init__.py``). A route's address and guards need all three, so
each file's wiring (:func:`file_wiring`) is collected once per repository and composed here:

* an app (``Flask(…)``, at module level or inside ``create_app()``) is the root: ``/``;
* a blueprint is served where it is registered, under the registration's ``url_prefix`` when it
  gives one, else the blueprint's own (Flask's rule); a blueprint registered on another joins
  its prefix to the parent's;
* a blueprint registered nowhere the code shows (a loop over a list, a variable), or at two
  places, or under a prefix that is not a literal, has an unknown address (``None``) — never its
  own prefix alone;
* ``before_request`` hooks are guards of the routes of the app or blueprint they are registered
  on (and of the blueprints registered on it); ``before_app_request`` hooks, of the whole app.

The REST extensions add their own containers, composed the same way:

* a Flask-RESTful / Flask-RESTX ``Api(app_or_blueprint, prefix=…)`` (or ``api.init_app(app)``)
  serves its resources at the app's or blueprint's prefix + its own ``prefix``;
* a Flask-RESTX ``Namespace("cats", path=…)`` (or ``api.namespace(…)``) added to one ``Api``
  (``api.add_namespace(ns, path=…)``) serves at the Api's prefix + its path (``/cats`` by default);
* a Flask-Smorest ``Blueprint`` is a Flask blueprint; its ``Api(app).register_blueprint(blp)``
  registers it on the app.

Their prefixes are joined to a resource's URL by plain concatenation, as the extensions do
(:data:`FlaskWiring.resources`), not by Flask's ``url_prefix`` join.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..python_common import (
    Origins, calls, field_text, import_refs, keyword, literal, positional, string_list,
)
from ..treesitter import node_text

FACT = "flask.wiring"

#: An app or blueprint: (file, variable).
Key = tuple[str, str]
#: A variable as one file refers to it: ``("local", var)`` or ``("ext", file, var)``.
Ref = tuple[str, ...]
#: A ``url_prefix``: ``("lit", value)`` or ``("unknown",)``.
Spec = tuple[str, ...]

_APP_CLASSES = frozenset({"flask.Flask"})
_BLUEPRINT_CLASSES = frozenset({"flask.Blueprint", "flask.blueprints.Blueprint",
                                "flask_smorest.Blueprint", "flask_smorest.blueprint.Blueprint"})
#: The REST extensions' ``Api`` classes → which extension.
API_CLASSES = {"flask_restful.Api": "restful", "flask_restx.Api": "restx", "flask_restx.api.Api": "restx",
               "flask_smorest.Api": "smorest"}
_NAMESPACE_CLASSES = frozenset({"flask_restx.Namespace", "flask_restx.namespace.Namespace"})
#: HTTP verbs a ``MethodView`` dispatches to a method of the same name.
VERBS = ("get", "post", "put", "patch", "delete")


@dataclass
class ViewClass:
    """A class-based view: where it is declared and the HTTP methods it handles."""

    line: int
    #: verb method → its line (``MethodView``: ``def get(self)`` → ``{"get": 12}``).
    verbs: dict[str, int] = field(default_factory=dict)
    #: ``methods = ["GET", "POST"]`` on the class; None when absent or not a literal.
    methods: tuple[str, ...] | None = None
    #: Line of ``dispatch_request`` (a plain ``View``), None when not defined here.
    dispatch: int | None = None


@dataclass
class FileWiring:
    """What one file contributes to the repo's Flask wiring. Plain data (picklable)."""

    apps: set[str] = field(default_factory=set)
    #: Blueprint variable → its own ``url_prefix``.
    blueprints: dict[str, Spec] = field(default_factory=dict)
    #: (receiver, blueprint registered, the registration's ``url_prefix`` — None when not given).
    registrations: list[tuple[Ref, Ref, Spec | None]] = field(default_factory=list)
    #: (app or blueprint, hook function, whether it covers the whole app — ``before_app_request``).
    hooks: list[tuple[Ref, str, bool]] = field(default_factory=list)
    #: Class-based views declared at module level.
    classes: dict[str, ViewClass] = field(default_factory=dict)
    #: REST extension ``Api`` variable → (extension, the app or blueprint it is made with — None
    #: when not given, its ``prefix``).
    apis: dict[str, tuple[str, Ref | None, Spec]] = field(default_factory=dict)
    #: ``x.init_app(app)``: (x, app). Kept for every receiver; only an ``Api``'s count.
    inits: list[tuple[Ref, Ref]] = field(default_factory=list)
    #: Flask-RESTX namespace variable → (its own path — ``/`` + name by default, whether it is
    #: made by ``Namespace(…)``; else by ``x.namespace(…)``, a namespace only if ``x`` is an Api).
    namespaces: dict[str, tuple[Spec, bool]] = field(default_factory=dict)
    #: (Api, namespace added to it, the ``path`` it is added at — None when not given).
    attached: list[tuple[Ref, Ref, Spec | None]] = field(default_factory=list)


@dataclass(frozen=True)
class FlaskWiring:
    #: File → {app / blueprint variable → (served prefix or None when unknown, guards)}.
    served: dict[str, dict[str, tuple[str | None, tuple[str, ...]]]] = field(default_factory=dict)
    #: File → its class-based views.
    classes: dict[str, dict[str, ViewClass]] = field(default_factory=dict)
    #: File → {Flask-RESTful / RESTX ``Api`` or RESTX namespace variable → (the prefix its
    #: resources' URLs are appended to — None when unknown, guards)}.
    resources: dict[str, dict[str, tuple[str | None, tuple[str, ...]]]] = field(default_factory=dict)


def flask_wiring(index: Any | None) -> FlaskWiring | None:
    found: FlaskWiring | None = index_fact(index, FACT)
    return found


def join(prefix: str, rule: str) -> str:
    """Flask's join of a ``url_prefix`` and a rule (or of two prefixes)."""
    if prefix and rule:
        return prefix.rstrip("/") + "/" + rule.lstrip("/")
    return prefix or rule


def ref_of(node: Node | None, source: bytes, rel: str, local: set[str],
           imports: dict[str, tuple[str, ...]]) -> Ref | None:
    """How ``node`` (``bp`` / ``auth.bp``) names an app or blueprint: a variable of this file, a
    name imported from a repo file, or an attribute of an imported repo module."""
    if node is None:
        return None
    if node.type == "identifier":
        name = node_text(node, source)
        if name in local:
            return ("local", name)
        ref = imports.get(name)
        if ref is not None and ref[0] == "symbol":
            return ("ext", ref[1], ref[2])
        return ("local", name)
    if node.type == "attribute":
        obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
        ref = imports.get(node_text(obj, source)) if obj is not None and obj.type == "identifier" else None
        if ref is not None and ref[0] == "module" and attr is not None:
            return ("ext", ref[1], node_text(attr, source))
    return None


def key_of(ref: Ref, rel: str) -> Key:
    return (rel, ref[1]) if ref[0] == "local" else (ref[1], ref[2])


def _spec(call: Node, source: bytes) -> Spec | None:
    value = keyword(call, source, "url_prefix")
    if value is None or value.type == "none":
        return None
    text = literal(value, source)
    return ("lit", text) if text is not None else ("unknown",)


def _literal_spec(value: Node | None, source: bytes, default: str | None) -> Spec:
    if value is None or value.type == "none":
        return ("lit", default) if default is not None else ("unknown",)
    text = literal(value, source)
    return ("lit", text) if text is not None else ("unknown",)


def _namespace_path(call: Node, source: bytes) -> Spec:
    """A RESTX namespace's path: ``path=``, else ``/`` + its name, without a trailing ``/``."""
    path = keyword(call, source, "path")
    if path is not None and path.type != "none":
        text = literal(path, source)
        return ("lit", text.rstrip("/")) if text is not None else ("unknown",)
    args = positional(call)
    name = literal(args[0] if args else keyword(call, source, "name"), source)
    return ("lit", ("/" + name).rstrip("/")) if name is not None else ("unknown",)


def _attribute_call(call: Node, source: bytes) -> str | None:
    fn = call.child_by_field_name("function")
    return field_text(fn, "attribute", source) if fn is not None and fn.type == "attribute" else None


def _api_kind(call: Node, source: bytes, origins: Origins) -> str | None:
    fn = call.child_by_field_name("function")
    return API_CLASSES.get(origins.qualified(node_text(fn, source)) or "") if fn is not None else None


def argument_ref(node: Node | None, source: bytes, rel: str, local: set[str],
                 imports: dict[str, tuple[str, ...]]) -> Ref | None:
    """:func:`ref_of`, for an argument: a name only (a call or a literal is not a variable)."""
    return ref_of(node, source, rel, local, imports) if node is not None and node.type in ("identifier", "attribute") else None


def _is(call: Node, source: bytes, origins: Origins, classes: frozenset[str]) -> bool:
    fn = call.child_by_field_name("function")
    if fn is None:
        return False
    written = node_text(fn, source)
    return written in classes or origins.qualified(written) in classes


def _assignment(node: Node) -> Node | None:
    """``node`` as an assignment, unwrapping an ``expression_statement``."""
    if node.type == "expression_statement" and node.named_children:
        node = node.named_children[0]
    return node if node.type == "assignment" else None


def view_classes(root: Node, source: bytes) -> dict[str, ViewClass]:
    """Module-level classes with ``as_view`` dispatch: verb methods, ``methods``,
    ``dispatch_request``. Every class is read; only the ones a route names are used."""
    out: dict[str, ViewClass] = {}
    for stmt in root.named_children:
        cdef = stmt.child_by_field_name("definition") if stmt.type == "decorated_definition" else stmt
        if cdef is None or cdef.type != "class_definition":
            continue
        name, body = cdef.child_by_field_name("name"), cdef.child_by_field_name("body")
        if name is None or body is None:
            continue
        view = ViewClass(line=cdef.start_point[0] + 1)
        for member in body.named_children:
            fdef = member.child_by_field_name("definition") if member.type == "decorated_definition" else member
            if fdef is not None and fdef.type == "function_definition":
                fname = field_text(fdef, "name", source)
                if fname in VERBS:
                    view.verbs[fname] = fdef.start_point[0] + 1
                elif fname == "dispatch_request":
                    view.dispatch = fdef.start_point[0] + 1
            elif (assign := _assignment(member)) is not None:
                left = assign.child_by_field_name("left")
                if left is not None and node_text(left, source) == "methods":
                    values = string_list(assign.child_by_field_name("right"), source)
                    view.methods = tuple(v.upper() for v in values) if values is not None else None
        out[node_text(name, source)] = view
    return out


def file_wiring(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileWiring:
    """The Flask wiring of one file (see :class:`FileWiring`)."""
    origins = Origins(root, source)
    imports = import_refs(root, source, repo_root, rel, anywhere=True) if repo_root is not None else {}
    out = FileWiring()
    all_calls = calls(root)
    for call in all_calls:
        parent = call.parent
        if parent is None or parent.type != "assignment" or parent.child_by_field_name("right") != call:
            continue
        target = parent.child_by_field_name("left")
        if target is None or target.type != "identifier":
            continue
        name = node_text(target, source)
        if _is(call, source, origins, _APP_CLASSES):
            out.apps.add(name)
        elif _is(call, source, origins, _BLUEPRINT_CLASSES):
            spec = _spec(call, source)
            out.blueprints[name] = spec if spec is not None else ("lit", "")
        elif (kind := _api_kind(call, source, origins)) is not None:
            out.apis[name] = (kind, None, _literal_spec(keyword(call, source, "prefix"), source, ""))
        elif _is(call, source, origins, _NAMESPACE_CLASSES):
            out.namespaces[name] = (_namespace_path(call, source), True)
        elif _attribute_call(call, source) == "namespace":  # ns = api.namespace("todos")
            out.namespaces[name] = (_namespace_path(call, source), False)
    local = out.apps | set(out.blueprints) | set(out.apis) | set(out.namespaces)
    for call in all_calls:  # Api(app): the app, once every local name is known
        parent = call.parent
        target = parent.child_by_field_name("left") if parent is not None and parent.type == "assignment" else None
        name = node_text(target, source) if target is not None and target.type == "identifier" else ""
        if name in out.apis and parent is not None and parent.child_by_field_name("right") == call:
            args = positional(call)
            app = argument_ref(args[0] if args else keyword(call, source, "app"), source, rel, local, imports)
            out.apis[name] = (out.apis[name][0], app, out.apis[name][2])
        elif name in out.namespaces and _attribute_call(call, source) == "namespace":
            fn = call.child_by_field_name("function")
            api = ref_of(fn.child_by_field_name("object") if fn is not None else None, source, rel, local, imports)
            if api is not None:
                out.attached.append((api, ("local", name), None))
    for call in all_calls:
        fn = call.child_by_field_name("function")
        attr = fn.child_by_field_name("attribute") if fn is not None and fn.type == "attribute" else None
        if attr is None:
            continue
        method = node_text(attr, source)
        receiver = ref_of(fn.child_by_field_name("object") if fn is not None else None,
                          source, rel, local, imports)
        args = positional(call)
        if receiver is None or not args:
            continue
        if method == "register_blueprint":
            child = ref_of(args[0], source, rel, local, imports)
            if child is not None:
                out.registrations.append((receiver, child, _spec(call, source)))
        elif method == "init_app":
            app = ref_of(args[0], source, rel, local, imports)
            if app is not None:
                out.inits.append((receiver, app))
        elif method == "add_namespace":
            ns = ref_of(args[0], source, rel, local, imports)
            path = keyword(call, source, "path") or (args[1] if len(args) > 1 else None)
            if ns is not None:
                out.attached.append((receiver, ns, _literal_spec(path, source, None) if path is not None else None))
        elif method in ("before_request", "before_app_request") and args[0].type == "identifier":
            out.hooks.append((receiver, node_text(args[0], source), method == "before_app_request"))
    for stmt in root.named_children:  # @app.before_request on a function
        _decorator_hooks(stmt, source, rel, local, imports, out)
    out.classes = view_classes(root, source)
    return out


def _decorator_hooks(node: Node, source: bytes, rel: str, local: set[str],
                     imports: dict[str, tuple[str, ...]], out: FileWiring) -> None:
    for child in [node] if node.type == "decorated_definition" else []:
        fdef = child.child_by_field_name("definition")
        if fdef is None or fdef.type != "function_definition":
            continue
        for dec in (c for c in child.named_children if c.type == "decorator"):
            expr = dec.named_children[0] if dec.named_children else None
            if expr is None or expr.type != "attribute":
                continue
            hook = field_text(expr, "attribute", source)
            if hook in ("before_request", "before_app_request"):
                receiver = ref_of(expr.child_by_field_name("object"), source, rel, local, imports)
                if receiver is not None:
                    out.hooks.append((receiver, field_text(fdef, "name", source),
                                      hook == "before_app_request"))
    if node.type in ("function_definition", "decorated_definition"):  # create_app()
        body = (node.child_by_field_name("definition") or node).child_by_field_name("body")
        for inner in body.named_children if body is not None else []:
            _decorator_hooks(inner, source, rel, local, imports, out)


def resolve_wiring(files: dict[str, FileWiring]) -> FlaskWiring:
    """Compose the files' wiring: each app's and blueprint's served prefix and guards."""
    apps = {(rel, v) for rel, f in files.items() for v in f.apps}
    blueprints = {(rel, v): s for rel, f in files.items() for v, s in f.blueprints.items()}
    apis = _api_targets(files)
    sites: dict[Key, list[tuple[Key, Spec | None]]] = {}
    for rel, f in files.items():
        for receiver, child, spec in f.registrations:
            target, at = key_of(child, rel), key_of(receiver, rel)
            if at in apis:  # Flask-Smorest's Api(app).register_blueprint(blp): on the app
                at = apis[at][1] or _UNKNOWN
            if target in blueprints:
                sites.setdefault(target, []).append((at, spec))
    own_hooks: dict[Key, list[str]] = {}
    app_wide: dict[Key, list[str]] = {}  # blueprint → its before_app_request hooks
    for rel, f in files.items():
        for receiver, name, whole_app in f.hooks:
            (app_wide if whole_app else own_hooks).setdefault(key_of(receiver, rel), []).append(name)

    memo: dict[Key, tuple[str | None, tuple[str, ...], Key | None]] = {}

    def served(node: Key, seen: frozenset[Key]) -> tuple[str | None, tuple[str, ...], Key | None]:
        if node in memo:
            return memo[node]
        hooks = tuple(own_hooks.get(node, ()))
        result: tuple[str | None, tuple[str, ...], Key | None]
        if node in apps:
            result = ("", hooks, node)
        elif node not in blueprints:
            result = (None, (), None)
        else:
            at = sites.get(node, [])
            if len(at) != 1 or node in seen:
                result = (None, hooks, None)
            else:
                parent, spec = at[0]
                base, guards, root = served(parent, seen | {node})
                own = spec if spec is not None else blueprints[node]
                prefix = join(base, own[1]) if base is not None and own[0] == "lit" else None
                result = (prefix, guards + hooks, root)
        if not seen:
            memo[node] = result
        return result

    resolved = {node: served(node, frozenset()) for node in sorted(apps | set(blueprints))}
    resources = _resources(files, apis, resolved)
    # before_app_request hooks of every blueprint served under an app guard all of that app.
    whole: dict[Key, list[str]] = {}
    for node in sorted(app_wide):
        root = resolved.get(node, (None, (), None))[2]
        if root is not None:
            whole.setdefault(root, []).extend(app_wide[node])
    out: dict[str, dict[str, tuple[str | None, tuple[str, ...]]]] = {}
    for (rel, var), (prefix, guards, root) in resolved.items():
        guards = tuple(whole.get(root, ())) + guards if root is not None else guards
        out.setdefault(rel, {})[var] = (prefix, tuple(dict.fromkeys(guards)))
    return FlaskWiring(served=out, classes={rel: f.classes for rel, f in files.items() if f.classes},
                       resources=resources)


#: A container the code does not show: served nowhere known.
_UNKNOWN: Key = ("", "")


def _api_targets(files: dict[str, FileWiring]) -> dict[Key, tuple[str, Key | None, Spec]]:
    """Every REST extension ``Api`` → (extension, the app or blueprint it serves — given to the
    constructor, else the one ``init_app`` call; None when unknown, its ``prefix``)."""
    inits: dict[Key, list[Key]] = {}
    for rel, f in files.items():
        for receiver, app in f.inits:
            inits.setdefault(key_of(receiver, rel), []).append(key_of(app, rel))
    out: dict[Key, tuple[str, Key | None, Spec]] = {}
    for rel, f in files.items():
        for var, (kind, target, prefix) in f.apis.items():
            given = [key_of(target, rel)] if target is not None else []
            at = given + inits.get((rel, var), [])
            out[(rel, var)] = (kind, at[0] if len(at) == 1 else None, prefix)
    return out


def _resources(files: dict[str, FileWiring], apis: dict[Key, tuple[str, Key | None, Spec]],
               resolved: dict[Key, tuple[str | None, tuple[str, ...], Key | None]]
               ) -> dict[str, dict[str, tuple[str | None, tuple[str, ...]]]]:
    """The prefix each Flask-RESTful / RESTX Api and RESTX namespace appends its resources' URLs
    to: the served prefix of the app or blueprint + the Api's ``prefix`` (+ the namespace's path)."""
    prefixes: dict[Key, tuple[str | None, tuple[str, ...]]] = {}
    for key, (kind, target, prefix) in apis.items():
        if kind == "smorest":
            continue
        base, guards, _ = resolved.get(target, (None, (), None)) if target is not None else (None, (), None)
        prefixes[key] = (base + prefix[1] if base is not None and prefix[0] == "lit" else None, guards)
    added: dict[Key, list[tuple[Key, Spec | None]]] = {}
    for rel, f in files.items():
        for api, ns, path in f.attached:
            added.setdefault(key_of(ns, rel), []).append((key_of(api, rel), path))
    for rel, f in files.items():
        for var, (own, certain) in f.namespaces.items():
            at = added.get((rel, var), [])
            restx = [(api, path) for api, path in at if apis.get(api, ("",))[0] == "restx"]
            if not certain and not restx:
                continue  # x.namespace(…) on something that is not a RESTX Api
            ns_prefix: str | None = None
            ns_guards: tuple[str, ...] = ()
            if len(at) == 1 and restx:
                (api, path) = restx[0]
                api_prefix, ns_guards = prefixes[api]
                spec = path if path is not None else own
                ns_prefix = api_prefix + spec[1] if api_prefix is not None and spec[0] == "lit" else None
            prefixes[(rel, var)] = (ns_prefix, ns_guards)
    out: dict[str, dict[str, tuple[str | None, tuple[str, ...]]]] = {}
    for (rel, var), value in sorted(prefixes.items()):
        out.setdefault(rel, {})[var] = value
    return out


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> tuple[str, FileWiring] | None:
    facts = file_wiring(root, source, rel, repo_root)
    if not (facts.apps or facts.blueprints or facts.registrations or facts.hooks or facts.classes
            or facts.apis or facts.namespaces or facts.attached or facts.inits):
        return None
    return rel, facts


def _resolve(values: list[tuple[str, FileWiring]], index: Any) -> FlaskWiring | None:
    if not values:
        return None
    return resolve_wiring(dict(values))


register_fact(FactSpec(
    name=FACT, language="python", gate=(b"flask",), collect=_collect, resolve=_resolve,
))
