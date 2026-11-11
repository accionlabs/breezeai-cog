"""Flask route detection — the ``flask`` additive detector: ``@app.route`` / ``@bp.get``
decorators and ``add_url_rule`` calls, and the REST extensions' resources: Flask-RESTful /
Flask-RESTX ``add_resource`` calls, Flask-RESTX ``@ns.route`` and Flask-Smorest ``@blp.route`` on a
class.

Each binding gets one ``route`` record per HTTP method (role table:
``docs/entry-point-roles/flask.md``). The address is the rule joined to the prefix the app or
blueprint is served under — from the ``flask.wiring`` fact when the repo index is there (it
follows ``register_blueprint`` across files), else from this file's own wiring.

It runs on every Python file, whichever parser owns it, so it records a route only when it is
certainly Flask's: the receiver is a Flask app or blueprint (``app = Flask(…)``, ``bp =
Blueprint(…)``, here or imported), or — for a receiver the code does not resolve (``app =
create_app()``) — the file imports Flask and not FastAPI, whose decorators look the same.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ...emit import function_id
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..handler_sites import handler_location
from ..python_common import (
    Origins, argument, field_text, import_refs, keyword, literal, positional, receiver, string_list,
)
from ..treesitter import node_text
from .wiring import (
    VERBS, FlaskWiring, ViewClass, file_wiring, flask_wiring, join, key_of, ref_of, resolve_wiring,
)

FRAMEWORK = "flask"
#: Decorator methods that register a route: ``route`` (``methods=``) and the Flask 2.0 verbs.
_ROUTE_METHODS = frozenset({"route", *VERBS})
#: Flask-Login decorators that reject an anonymous user before the view runs.
CERTAIN_AUTH = frozenset({
    "flask_login.login_required", "flask_login.fresh_login_required",
    "flask_login.utils.login_required", "flask_login.utils.fresh_login_required",
})
#: Bytes a file with a Flask route or a Flask import must contain (the detector's own guard).
_MARKERS = (b"flask", b"route(", b"add_url_rule", b"add_resource", *(f".{v}(".encode() for v in VERBS))
#: The REST extensions whose ``add_resource`` / class ``route`` a receiver the code does not
#: resolve may belong to.
_REST_PACKAGES = ("flask_restful", "flask_restx", "flask_smorest")


class _File:
    """What every binding of one file needs: its imports, the served prefix and guards of each
    app and blueprint, its class-based views and its module-level functions. Built only when the
    file has a route-shaped construct."""

    def __init__(self, dc: DetectContext, origins: Origins) -> None:
        root, source, path = dc.root, dc.source, dc.path
        self.source, self.path, self.origins = source, path, origins
        repo_root = Path(dc.ctx.repo_root) if dc.ctx.repo_root is not None else None
        self.repo_root = repo_root
        self.imports = import_refs(root, source, repo_root, path, anywhere=True, plain=True) if repo_root is not None else {}
        wiring: FlaskWiring | None = flask_wiring(dc.ctx.resolution_index)
        if wiring is None:  # no repo index: this file's own wiring only
            wiring = resolve_wiring({path: file_wiring(root, source, path, repo_root)})
        self.wiring = wiring
        self.local = set(wiring.served.get(path, {}))
        self.classes = wiring.classes.get(path, {})
        self.function_ids = {f.id for f in dc.record.functions}
        # A receiver the code does not resolve is Flask's only in a Flask (not FastAPI) file.
        self.flask_file = origins.imports_package("flask") and not origins.imports_package("fastapi")
        self.rest_file = any(origins.imports_package(p) for p in _REST_PACKAGES)
        self.local_resources = set(wiring.resources.get(path, {}))
        self.functions: dict[str, tuple[int, list[str]]] = {}
        for stmt in root.named_children:
            fdef = stmt.child_by_field_name("definition") if stmt.type == "decorated_definition" else stmt
            if fdef is not None and fdef.type == "function_definition":
                decorators = [d for d in stmt.named_children if d.type == "decorator"]
                self.functions[field_text(fdef, "name", source)] = (
                    fdef.start_point[0] + 1,
                    [n for d in decorators if (n := _decorator_name(d, source)) is not None])

    def served(self, receiver: Node | None) -> tuple[str | None, tuple[str, ...]] | None:
        """(served prefix — None when unknown, guards) of the app or blueprint ``receiver``;
        ``(None, ())`` for a receiver the code does not resolve, in a Flask file; None when the
        receiver is not Flask's."""
        found = self.container(receiver)
        if found is not None:
            return found
        return (None, ()) if self.flask_file else None

    def container(self, receiver: Node | None) -> tuple[str | None, tuple[str, ...]] | None:
        """:meth:`served` for an app or blueprint the code shows; None otherwise."""
        ref = ref_of(receiver, self.source, self.path, self.local, self.imports, self.repo_root)
        if ref is None:
            return None
        file, var = key_of(ref, self.path)
        return self.wiring.served.get(file, {}).get(var)

    def resource_prefix(self, receiver: Node | None) -> tuple[str | None, tuple[str, ...]] | None:
        """(the prefix the URLs of a Flask-RESTful / RESTX Api or RESTX namespace ``receiver``
        are appended to — None when unknown, guards); ``(None, ())`` for a receiver the code
        does not resolve, in a file of those extensions; None when it is not one."""
        ref = ref_of(receiver, self.source, self.path, self.local_resources, self.imports, self.repo_root)
        if ref is not None:
            file, var = key_of(ref, self.path)
            found = self.wiring.resources.get(file, {}).get(var)
            if found is not None:
                return found
            if self.wiring.served.get(file, {}).get(var) is not None:
                return None  # an app or a blueprint: not an extension's container
        return (None, ()) if self.rest_file else None

    def view_class(self, node: Node) -> tuple[str, ViewClass, str] | None:
        """(name, view, declaring file) of the class ``node`` names, in this file or imported."""
        name = node_text(node, self.source)
        if node.type == "identifier" and name in self.classes:
            return name, self.classes[name], self.path
        ref = ref_of(node, self.source, self.path, set(), self.imports, self.repo_root)
        found = self.wiring.classes.get(ref[1], {}).get(ref[2]) if ref is not None and ref[0] == "ext" else None
        return (ref[2], found, ref[1]) if ref is not None and found is not None else None

    def certain(self, guards: list[str]) -> bool:
        return any(self.origins.qualified(g) in CERTAIN_AUTH for g in guards)

    def parent(self, name: str, line: int, class_name: str | None) -> str | None:
        fid = function_id(self.path, name, line, class_name=class_name)
        return fid if fid in self.function_ids else None


def _decorator_name(dec: Node, source: bytes) -> str | None:
    """A decorator as written, without its arguments (``roles_required("admin")`` →
    ``roles_required``)."""
    expr = dec.named_children[0] if dec.named_children else None
    if expr is not None and expr.type == "call":
        expr = expr.child_by_field_name("function")
    if expr is None or expr.type not in ("identifier", "attribute"):
        return None
    return node_text(expr, source)


def _route_call(dec: Node, source: bytes) -> tuple[Node, str, str] | None:
    """(call, decorator method, rule) of a route decorator ``@x.route("/r")``."""
    call = dec.named_children[0] if dec.named_children else None
    fn = call.child_by_field_name("function") if call is not None and call.type == "call" else None
    attr = fn.child_by_field_name("attribute") if fn is not None and fn.type == "attribute" else None
    if call is None or attr is None or node_text(attr, source) not in _ROUTE_METHODS:
        return None
    args = positional(call)
    rule = literal(args[0], source) if args else None
    if rule is None or not rule.startswith("/"):  # Werkzeug rules start with "/"
        return None
    return call, node_text(attr, source), rule


def _call_attribute(call: Node, source: bytes) -> str | None:
    """The method a call is made with (``api.add_resource(…)`` → ``add_resource``)."""
    fn = call.child_by_field_name("function")
    attr = fn.child_by_field_name("attribute") if fn is not None and fn.type == "attribute" else None
    return node_text(attr, source) if attr is not None else None


def _attribute_name(dec: Node, source: bytes) -> str | None:
    """The method a decorator call is made with (``@ns.route("/")`` → ``route``)."""
    call = dec.named_children[0] if dec.named_children else None
    return _call_attribute(call, source) if call is not None and call.type == "call" else None


def _is_add_url_rule(call: Node, source: bytes) -> bool:
    return _call_attribute(call, source) == "add_url_rule"


def _methods(value: Node | None, source: bytes, default: list[str | None]) -> list[str | None]:
    """The verbs of a ``methods=`` argument: ``default`` when absent, one unknown verb (None)
    when it is not a literal list."""
    if value is None:
        return default
    values = string_list(value, source)
    return [v.upper() for v in dict.fromkeys(values)] if values else [None]


def _route(node: Node, method: str | None, endpoint: str | None, handler: str | None,
           line: int | None, handler_path: str | None, guards: list[str], certain: bool,
           parent: str | None = None, node_type: str | None = None) -> Emit:
    """One route record. A call-based binding (``add_url_rule``) enriches the call's own
    statement; a decorator route is appended (``synthetic``) under its function."""
    return Emit(node, dict(
        semanticType="route", method=method, endpoint=endpoint or None, framework=FRAMEWORK,
        handler=handler or None, handlerLine=line, handlerPath=handler_path,
        guards=list(dict.fromkeys(guards)) or None, authRequired=True if certain else None,
        routeKind="route",
    ), parent=parent, node_type=node_type, enrich="never" if node_type else "own")


def _decorated(f: _File, decorated: Node, fdef: Node, class_name: str | None) -> list[Emit]:
    source = f.source
    name = field_text(fdef, "name", source)
    line = fdef.start_point[0] + 1
    decorators = [c for c in decorated.named_children if c.type == "decorator"]
    out: list[Emit] = []
    for i, dec in enumerate(decorators):
        found = _route_call(dec, source)
        if found is None:
            continue
        call, verb, rule = found
        served = f.served(receiver(call))
        if served is None:
            continue
        prefix, inherited = served
        # Only the decorators below the route wrap the view it registers.
        below = [n for d in decorators[i + 1:]
                 if _route_call(d, source) is None and (n := _decorator_name(d, source)) is not None]
        endpoint = join(prefix, rule) if prefix is not None else None
        methods = [verb.upper()] if verb != "route" else _methods(keyword(call, source, "methods"), source, ["GET"])
        out.extend(_route(dec, m, endpoint, name, line, None, [*inherited, *below], f.certain(below),
                          f.parent(name, line, class_name), node_type="synthetic") for m in methods)
    return out


def _class_view(f: _File, call: Node, view: Node, endpoint: str | None, guards: list[str],
                listed: list[str] | None) -> list[Emit]:
    """``add_url_rule(rule, view_func=Cls.as_view("name"))``: one record per method."""
    source = f.source
    callee = view.child_by_field_name("function")
    attr = callee.child_by_field_name("attribute") if callee is not None and callee.type == "attribute" else None
    if attr is None or node_text(attr, source) != "as_view":
        return []
    target = callee.child_by_field_name("object") if callee is not None else None
    if target is None:
        return []
    found = f.view_class(target)
    if found is None:  # a class the code does not show: its methods are unknown
        unknown: list[str | None] = list(listed) if listed else [None]
        return [_route(call, m, endpoint, node_text(target, source).rsplit(".", 1)[-1], None, None,
                       guards, False) for m in unknown]
    name, cls, file = found
    if cls.verbs:  # MethodView: each verb is its own method
        verbs = [v.lower() for v in listed if v.lower() in cls.verbs] if listed else list(cls.verbs)
        return [_route(call, v.upper(), endpoint, v, *handler_location((file, cls.verbs[v]), f.path),
                       guards, False) for v in verbs]
    methods: list[str | None] = (list(listed) if listed else list(cls.methods) if cls.methods
                                 else ["GET"] if cls.dispatch is not None else [None])
    handler, line = ("dispatch_request", cls.dispatch) if cls.dispatch is not None else (name, cls.line)
    return [_route(call, m, endpoint, handler, *handler_location((file, line), f.path), guards, False)
            for m in methods]


def _add_url_rule(f: _File, call: Node) -> list[Emit]:
    source = f.source
    rule = literal(argument(call, source, 0, "rule"), source)
    view = argument(call, source, 2, "view_func")
    if rule is None or not rule.startswith("/") or view is None:
        return []
    served = f.served(receiver(call))
    if served is None:
        return []
    prefix, inherited = served
    endpoint = join(prefix, rule) if prefix is not None else None
    explicit = keyword(call, source, "methods")
    if view.type == "call":
        listed = string_list(explicit, source) if explicit is not None else None
        return _class_view(f, call, view, endpoint, list(inherited), [m.upper() for m in listed] if listed else None)
    if view.type not in ("identifier", "attribute"):
        return []
    name = node_text(view, source)
    line, decorators = f.functions.get(name, (None, [])) if view.type == "identifier" else (None, [])
    return [_route(call, m, endpoint, name.rsplit(".", 1)[-1], line, None, [*inherited, *decorators],
                   f.certain(decorators)) for m in _methods(explicit, source, ["GET"])]


def _resource_records(f: _File, node: Node, cls: tuple[str, ViewClass, str] | None, written: str,
                      endpoints: list[str | None], guards: tuple[str, ...], synthetic: bool) -> list[Emit]:
    """One record per URL and HTTP method of a resource class (a ``MethodView``): its verb
    methods; one record with no method, at the class, when it defines none here; one record
    named as written when the class is not in the repo."""
    node_type = "synthetic" if synthetic else None
    out: list[Emit] = []
    for endpoint in endpoints:
        if cls is None:
            out.append(_route(node, None, endpoint, written.rsplit(".", 1)[-1], None, None, list(guards),
                              False, node_type=node_type))
            continue
        name, view, file = cls
        verbs = view.verbs or {"": view.line}
        out.extend(_route(node, verb.upper() or None, endpoint, verb or name,
                          *handler_location((file, line), f.path), list(guards), False, node_type=node_type)
                   for verb, line in verbs.items())
    return out


def _urls(args: list[Node], source: bytes, prefix: str | None, flask_join: bool) -> list[str | None]:
    """The address of each URL argument; None for one that is not a literal or a prefix that is
    not known."""
    out: list[str | None] = []
    for arg in args:
        url = literal(arg, source)
        if url is None or prefix is None:
            out.append(None)
        else:
            out.append(join(prefix, url) if flask_join else prefix + url)
    return out


def _add_resource(f: _File, call: Node) -> list[Emit]:
    """``api.add_resource(Todo, "/todos/<id>", "/todo/<id>")`` (Flask-RESTful, Flask-RESTX)."""
    args = positional(call)
    found = f.resource_prefix(receiver(call))
    if found is None or len(args) < 2 or args[0].type not in ("identifier", "attribute"):
        return []
    prefix, guards = found
    return _resource_records(f, call, f.view_class(args[0]), node_text(args[0], f.source),
                             _urls(args[1:], f.source, prefix, False), guards, False)


def _class_route(f: _File, decorated: Node, cdef: Node) -> list[Emit]:
    """``@ns.route("/<id>")`` (Flask-RESTX) / ``@blp.route("/")`` (Flask-Smorest) on a class."""
    source = f.source
    name = field_text(cdef, "name", source)
    out: list[Emit] = []
    for dec in (d for d in decorated.named_children if d.type == "decorator"):
        call = dec.named_children[0] if dec.named_children else None
        fn = call.child_by_field_name("function") if call is not None and call.type == "call" else None
        if call is None or fn is None or fn.type != "attribute" or field_text(fn, "attribute", source) != "route":
            continue
        args = positional(call)
        served = f.container(receiver(call))
        resource = f.resource_prefix(receiver(call)) if served is None else None
        if served is not None:  # a Flask-Smorest (Flask) blueprint
            endpoints, guards = _urls(args, source, served[0], True), served[1]
        elif resource is not None:
            endpoints, guards = _urls(args, source, resource[0], False), resource[1]
        else:
            continue
        view = f.classes.get(name)
        out.extend(_resource_records(f, dec, (name, view, f.path) if view is not None else None, name,
                                     endpoints, guards, True))
    return out


def _candidates(root: Node, source: bytes) -> list[tuple[str, Node, Node | None, str | None]]:
    """Route-shaped constructs, in source order: ``("decorated", definition, function,
    enclosing class)``, ``("class", definition, class, None)`` (a ``route`` decorator on a class),
    ``("call", add_url_rule call, None, None)`` and ``("resource", add_resource call, None,
    None)``. One walk; nothing is
    resolved yet."""
    out: list[tuple[str, Node, Node | None, str | None]] = []

    def walk(node: Node, class_name: str | None) -> None:
        for child in node.named_children:
            if child.type == "decorated_definition":
                inner = child.child_by_field_name("definition")
                if inner is not None and inner.type == "function_definition":
                    if any(_route_call(d, source) is not None
                           for d in child.named_children if d.type == "decorator"):
                        out.append(("decorated", child, inner, class_name))
                    walk(inner, class_name)
                elif inner is not None and inner.type == "class_definition":
                    if any(_attribute_name(d, source) == "route"
                           for d in child.named_children if d.type == "decorator"):
                        out.append(("class", child, inner, None))
                    walk(inner, field_text(inner, "name", source))
            elif child.type == "class_definition":
                walk(child, field_text(child, "name", source))
            else:
                if child.type == "call" and _is_add_url_rule(child, source):
                    out.append(("call", child, None, None))
                elif child.type == "call" and _call_attribute(child, source) == "add_resource":
                    out.append(("resource", child, None, None))
                walk(child, class_name)

    walk(root, None)
    return out


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    if not any(m in source for m in _MARKERS):
        return None
    origins = Origins(dc.root, source)
    imports_flask = origins.imports_package("flask")
    candidates = _candidates(dc.root, source)
    if not candidates:
        return Detection([], label=FRAMEWORK) if imports_flask else None
    f = _File(dc, origins)
    found: list[Emit] = []
    for kind, node, fdef, class_name in candidates:
        if kind == "decorated" and fdef is not None:
            found.extend(_decorated(f, node, fdef, class_name))
        elif kind == "class" and fdef is not None:
            found.extend(_class_route(f, node, fdef))
        elif kind == "resource":
            found.extend(_add_resource(f, node))
        else:
            found.extend(_add_url_rule(f, node))
    if not found and not imports_flask:
        return None
    return Detection(found, label=FRAMEWORK)


register_detector(Detector(
    name="flask",
    gate=Gate("python", order=20, skip_fixtures=True),  # guards itself: several markers
    detect=_detect,
    frameworks=(FRAMEWORK,),
))
