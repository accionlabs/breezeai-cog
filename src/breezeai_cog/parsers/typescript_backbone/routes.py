"""Backbone.js / Marionette route detection (additive, TypeScript/JavaScript).

One ``route`` record per binding, at the binding:

* **Route tables** - ``routes: {...}`` of a ``Router.extend`` / ``extends Router`` (and
  Marionette's ``appRoutes`` with its ``controller``), also when the table is a variable, is
  assembled from spreads, or is passed as ``new Router({ routes })``.
* **Programmatic routes** - ``router.route(path, name, callback)`` on a router instance, an
  alias of one, or ``this`` inside a router class.
* **History root** - ``Backbone.history.start({ root })`` prefixes every endpoint once, only
  when exactly one root is configured with a literal ``pushState: true``. The file's own call
  decides; a file without one takes the root all other calls in the repo agree on
  (``backbone.history-root`` fact), usually set in ``main.js``.

Paths are kept as Backbone writes them (``:param``, ``(optional)``, ``*splat``). A path that
cannot be resolved to a literal is never guessed: the entry or ``route()`` call is skipped.
"""

from __future__ import annotations

from ..additive import (DetectContext, Detection, Detector, FactSpec, Gate, index_fact,
                        register_detector, register_fact)
from ..emits import Emit
from .backbone import (_FRAMEWORK, _Class, _Values, _args, _key, _kind, _new_target,
                       _object_props, _scope, _text, _unwrap, _walk)

_ORDER = 62  # must be unique among the TypeScript detectors (backbone.py takes 61)
_TABLES = ("routes", "appRoutes")
_FUNCTIONS = ("function_expression", "function", "arrow_function")
_ROOT_FACT = "backbone.history-root"  # the root set by a history.start in another file


def _line(node) -> int:
    return node.start_point[0] + 1


# --------------------------------------------------------------------------- history root

def _history_roots(root, src: bytes, values: _Values) -> list:
    """Roots of the ``Backbone.history.start({ root })`` calls of a file, normalised (``/app/`` -> ``app``).

    A root that is not a literal is ``None`` (ambiguous). Only ``pushState: true`` calls count:
    a hash-based router (the default) lives after ``#``, not under the root.
    """
    found: list = []
    for node in _walk(root):
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        # property first: ``fn`` of a chained call spans the whole chain, so its text is costly
        if _text(fn.child_by_field_name("property"), src) != "start":
            continue
        if not _text(fn.child_by_field_name("object"), src).endswith("history"):
            continue
        args = _args(node)
        options = values.object(args[0]) if args else None
        props = _object_props(options, src) if options is not None else {}
        if "root" not in props:
            continue
        push = props.get("pushState")
        if push is None or _text(push, src) != "true":  # hash mode (the default) or not a literal
            continue
        value = values.string(props["root"])
        found.append(value.strip("/") if value is not None else None)
    return found


def _single_root(roots: list) -> str | None:
    """``/app`` when every root agrees on one non-empty value; else None (never a guess)."""
    only = set(roots)
    if len(only) != 1 or None in only:
        return None
    root = next(iter(only))
    return "/" + root if root else None


def _history_base(root, src: bytes, values: _Values, index_root: str | None) -> str:
    """The base path of the file's routes.

    The file's own ``history.start`` decides when it has one (ambiguous -> no base). Otherwise
    the root every other ``history.start`` of the repo agrees on, usually ``main.js``.
    """
    roots = _history_roots(root, src, values)
    return (_single_root(roots) or "") if roots else (index_root or "")


def _collect_root_fact(root, source: bytes, _rel: str, _repo_root) -> list | None:
    roots = _history_roots(root, source, _Values(root, source))
    return roots or None


def _resolve_root_fact(values: list, _index) -> dict | None:
    """``{"root": "/app"}`` when the whole repo agrees on one root, else nothing."""
    root = _single_root([r for roots in values for r in roots])
    return {"root": root} if root else None


def _join(base: str, path: str) -> str:
    tail = path.lstrip("/")
    if not tail:
        return base or "/"
    return f"{base}/{tail}"


# --------------------------------------------------------------------------- bindings

def _handler_fields(value, owner: dict, values: _Values, src: bytes) -> dict:
    """The handler a route points at, as the code writes it, plus its line when local."""
    name = values.string(value)
    if name is not None:
        fields: dict = {"handler": name}
        target = owner.get(name)
        if target is not None:
            fields["handlerLine"] = _line(target)
        return fields
    if value.type in _FUNCTIONS:
        return {"handlerLine": _line(value)}
    if value.type == "identifier":
        return {"handler": _text(value, src)}
    if value.type == "member_expression":
        return {"handler": _text(value.child_by_field_name("property"), src)}
    return {}


def _route_fields(base: str, path: str) -> dict:
    return {
        "semanticType": "route",
        "framework": _FRAMEWORK,
        "routeKind": "page",
        "endpoint": _join(base, path),
    }


def _table_node(prop):
    """The object a ``routes`` property holds, or the one its function returns (Backbone calls it)."""
    if prop is not None and (prop.type == "method_definition" or prop.type in _FUNCTIONS):
        body = prop.child_by_field_name("body")  # ``routes() {}`` / ``routes: function () {}`` / ``() => ({})``
        if body is not None and body.type == "statement_block":
            ret = next((n for n in body.named_children if n.type == "return_statement"), None)
            prop = ret.named_children[0] if ret is not None and ret.named_children else None
        else:
            prop = body
    return prop


def _table_routes(props: dict, cls: _Class | None, base: str, values: _Values, src: bytes) -> list[Emit]:
    """The bindings of ``routes`` / ``appRoutes`` among the properties of a router."""
    found = []
    for table_name in _TABLES:
        table = _table_node(props.get(table_name))
        if table is None:
            continue
        if table_name == "appRoutes":  # handlers are methods of the ``controller``
            controller = values.object(props.get("controller"))
            owner = _object_props(controller, src) if controller is not None else {}
        else:
            owner = cls.props if cls else props
        for pair in values.pairs(table):
            key = pair.child_by_field_name("key")
            if key is not None and key.type == "computed_property_name":  # [PATH]: 'handler'
                path = values.string(key.named_children[0]) if key.named_children else None
            else:
                path = _key(key, src)
            value = pair.child_by_field_name("value")
            if path is None or value is None:
                continue
            fields = _route_fields(base, path)
            fields.update(_handler_fields(value, owner, values, src))
            found.append(Emit(pair, fields))
    return found


def _route_call(call, args: list, cls: _Class | None, base: str, values: _Values,
                src: bytes) -> Emit | None:
    """``router.route(path, name, callback)`` / ``router.route(path, callback)``."""
    regex = args[0].type == "regex"
    path = _text(args[0], src) if regex else values.string(args[0])
    if path is None:  # a computed path has no address to record
        return None
    fields = _route_fields("" if regex else base, path)
    if regex:
        fields["isRegex"] = True
    owner = cls.props if cls else {}
    if len(args) >= 3:  # the callback; the name only names the ``route:name`` event
        fields.update(_handler_fields(args[2], owner, values, src))
    elif len(args) == 2:  # a callback, or just the name (the callback is then this[name])
        fields.update(_handler_fields(args[1], owner, values, src))
    return Emit(call, fields, enrich="same")


# --------------------------------------------------------------------------- detector

def _detect_routes(dc: DetectContext) -> Detection | None:
    root, src = dc.root, dc.source
    # The gate guard says "oute" (Router, routes, route(...)). It cannot say "Backbone or Marionette"
    # (a Marionette-only file never says "Backbone"), so the library is checked here.
    if b"ackbone" not in src and b"arionette" not in src and b"Mn." not in src:
        return None
    scope = _scope(dc)
    values = scope.values
    shared = index_fact(dc.ctx.resolution_index, _ROOT_FACT) or {}
    base = _history_base(root, src, values, shared.get("root"))

    found: list[Emit] = []
    for cls in scope.classes:
        if cls.kind == "Router":
            found.extend(_table_routes(cls.props, cls, base, values, src))

    for node in _walk(root):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = _args(node)
            if fn is None or fn.type != "member_expression" or not args:
                continue
            if _text(fn.child_by_field_name("property"), src) != "route":
                continue
            obj = _unwrap(fn.child_by_field_name("object"))
            target = scope.resolve(_text(obj, src), node) if obj is not None else None
            if target is None and obj is not None and obj.type == "new_expression":
                target = _new_target(obj, src, scope.by_name, scope.roots)  # ``new AppRouter().route(...)``
            if target is not None and target.kind == "Router":
                emit = _route_call(node, args, target.cls, base, values, src)
                if emit is not None:
                    found.append(emit)
        elif node.type == "new_expression":  # new AppRouter({ routes: {...} })
            ctor = _text(node.child_by_field_name("constructor"), src)
            cls = scope.by_name.get(ctor) or scope.by_name.get(ctor.rsplit(".", 1)[-1])
            if (cls.kind if cls else _kind(ctor, scope.roots)) != "Router":
                continue
            args = _args(node)
            options = values.object(args[0]) if args else None
            if options is not None:
                found.extend(_table_routes(_object_props(options, src), cls, base, values, src))

    # A table inherited or shared by several routers is one binding, at one place.
    unique: dict = {}
    for emit in found:
        key = (emit.node.start_byte, emit.node.end_byte, tuple(sorted(emit.fields.items())))
        unique.setdefault(key, emit)
    found = list(unique.values())
    return Detection(found, label=_FRAMEWORK) if found else None


register_fact(FactSpec(name=_ROOT_FACT, language="typescript", gate=(b"history.start",),
                       collect=_collect_root_fact, resolve=_resolve_root_fact))

register_detector(Detector(
    name="backbone-routes",
    gate=Gate("typescript", order=_ORDER, guard=b"oute", skip_fixtures=True),
    detect=_detect_routes,
    frameworks=(_FRAMEWORK,),
))
