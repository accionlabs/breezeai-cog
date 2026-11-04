"""Backbone.js / Marionette route detection (additive, TypeScript/JavaScript).

One ``route`` record per binding, at the binding:

* **Route tables** - ``routes: {...}`` of a ``Router.extend`` / ``extends Router`` (and
  Marionette's ``appRoutes`` with its ``controller``), also when the table is a variable, is
  assembled from spreads, or is passed as ``new Router({ routes })``.
* **Programmatic routes** - ``router.route(path, name, callback)`` on a router instance, an
  alias of one, or ``this`` inside a router class.
* **History root** - ``Backbone.history.start({ pushState: true, root })`` prefixes every
  endpoint once. The file's own calls decide; a file without one takes the root every call in
  the repo agrees on (``backbone.history-root`` fact), usually set in ``main.js``. A hash-based
  start, or one without ``root``, counts as no prefix, so two apps never share one's root.
* **Handlers** - a name (``'show'``) is a method of that router, located there or left without a
  line; a function given directly is located like any TypeScript handler (in the file, or through
  its import). The generic lookup is not used (``locates_handlers``): a top-level ``show``
  elsewhere in the file is not the router's method.

Paths are kept as Backbone writes them (``:param``, ``(optional)``, ``*splat``). A path that
cannot be resolved to a literal is never guessed: the entry or ``route()`` call is skipped.
"""

from __future__ import annotations

from typing import Callable

from ..additive import (DetectContext, Detection, Detector, FactSpec, Gate, index_fact,
                        register_detector, register_fact)
from ..emits import Emit
from ..handler_sites import handler_location
from ..typescript.handlers import handler_locator
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
    """The root each ``Backbone.history.start(...)`` call of a file puts its routes under,
    normalised (``/app/`` -> ``app``).

    Every call counts, so that two apps of one repo are never given one app's root: a hash-based
    start (the default; routes live after ``#``) or one without ``root`` puts them under ``""``.
    ``None`` when it is not known: options, ``pushState`` or ``root`` that are not literals.
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
        if args and options is None:  # ``start(opts)``: the options are not known
            found.append(None)
            continue
        props = _object_props(options, src) if options is not None else {}
        push = props.get("pushState")
        mode = _text(push, src) if push is not None else "false"
        if mode not in ("true", "false"):
            found.append(None)
        elif mode == "false" or "root" not in props:  # hash mode, or pushState at the site root
            found.append("")
        else:
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

#: name -> ``handlerLine`` / ``handlerPath`` of a function given by name, where found
_Locate = Callable[[str], dict]


def _locator(dc: DetectContext) -> _Locate:
    """Where a function named directly (``'x': showX``) is declared: in the file or through its
    import, as the TypeScript handler lookup finds it. Built on first use."""
    lookup = None

    def locate(name: str) -> dict:
        nonlocal lookup
        if lookup is None:
            lookup = handler_locator(
                dc.root, dc.source, dc.path, dc.ctx.repo_root, dc.ctx.resolution_index
            )
        hit = lookup(name, None)
        line, path = handler_location(hit[1], dc.path) if hit is not None else (None, None)
        return {k: v for k, v in (("handlerLine", line), ("handlerPath", path)) if v is not None}

    return locate


def _handler_fields(value, owner: dict, values: _Values, src: bytes, locate: _Locate) -> dict:
    """The handler a route points at, as the code writes it, plus where it is declared when
    known: a name is a method of the router (``owner``); a function given directly is looked up."""
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
        name = _text(value, src)
        return {"handler": name, **locate(name)}
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


def _table_routes(props: dict, cls: _Class | None, base: str, values: _Values, src: bytes,
                  locate: _Locate) -> list[Emit]:
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
            fields.update(_handler_fields(value, owner, values, src, locate))
            found.append(Emit(pair, fields))
    return found


def _route_call(call, args: list, cls: _Class | None, base: str, values: _Values,
                src: bytes, locate: _Locate) -> Emit | None:
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
        fields.update(_handler_fields(args[2], owner, values, src, locate))
    elif len(args) == 2:  # a callback, or just the name (the callback is then this[name])
        fields.update(_handler_fields(args[1], owner, values, src, locate))
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
    locate = _locator(dc)

    found: list[Emit] = []
    for cls in scope.classes:
        if cls.kind == "Router":
            found.extend(_table_routes(cls.props, cls, base, values, src, locate))

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
                emit = _route_call(node, args, target.cls, base, values, src, locate)
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
                found.extend(_table_routes(_object_props(options, src), cls, base, values, src, locate))

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
    # `'orders/:id': 'show'` names a method of the router; a top-level `show` is not it.
    locates_handlers=True,
))
