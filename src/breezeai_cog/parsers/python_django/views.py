"""Django views as the URL patterns need them, collected from every Django file into the
``django.views`` fact.

A URL pattern names its view (``path("orders/", views.OrderList.as_view())``) but the view — its
methods, its decorators, its base classes — is declared in another file, often on a base class in
a third. So each file's module-level functions and classes are collected once (:func:`file_views`,
plain picklable data) and a pattern's view is resolved when the pattern is parsed
(:func:`resolve_class`): the methods it handles, walking its bases in the repository the way
Python's method resolution does, and Django's generic views by their documented methods
(:mod:`.generics`). A base the code does not show leaves the view's methods unknown — never
guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..django_common import DJANGO_BASES, DJANGO_VIEWS
from ..python_common import ImportRefs, Origins, field_text, keyword, literal, positional, string_list
from ..treesitter import node_text
from . import drf
from .generics import (
    AUTH_MIXIN_MODULE, AUTH_MIXINS, CERTAIN_AUTH, DJANGO_VIEW_MODULES, LOGIN_NOT_REQUIRED,
    METHOD_DECORATORS, NOT_GUARDS, VERBS, short,
)

FACT = "django.views"

#: A guard as written, and where it is imported from (``""`` when not imported: the app's own).
Guard = tuple[str, str]
#: A base class: ``("repo", file, name)``, ``("lib", origin, written)`` or ``("unknown", written)``.
Base = tuple[str, ...]


@dataclass
class FunctionView:
    line: int
    #: Every decorator, outermost first.
    decorators: tuple[Guard, ...] = ()
    #: The verbs a ``require_*`` decorator allows; None when it accepts any verb.
    methods: tuple[str, ...] | None = None
    #: DRF ``@api_view([...])``: the verbs (``GET`` when no list is given); None when not one.
    api_view: tuple[str, ...] | None = None
    #: DRF ``@permission_classes([...])``; None when absent or not a literal list.
    permissions: tuple[Guard, ...] | None = None


@dataclass
class Method:
    line: int
    decorators: tuple[Guard, ...] = ()
    #: DRF ``@action(detail=…, methods=[…], url_path=…)``: (detail — None when it or the path is
    #: not a literal, verbs — empty when not a literal, url path — None for the method name);
    #: None when the method is not an action.
    action: tuple[bool | None, tuple[str, ...], str | None] | None = None


@dataclass
class ClassView:
    line: int
    bases: tuple[Base, ...] = ()
    methods: dict[str, Method] = field(default_factory=dict)
    #: ``@method_decorator(x, name="dispatch")`` on the class: guards of every method.
    dispatch_decorators: tuple[Guard, ...] = ()
    #: DRF class attributes. ``permission_classes``: None when not set here; ``unknown`` when set
    #: to something that is not a literal list.
    permissions: tuple[Guard, ...] | None = None
    permissions_unknown: bool = False
    serializer: str | None = None
    lookup_field: str | None = None
    lookup_url_kwarg: str | None = None
    #: The class assigns ``routes`` (``routes = […]``, ``self.routes = …``), names
    #: ``trailing_slash`` or passes a positional argument to its base's ``__init__`` (where DRF's
    #: routers take ``trailing_slash``): as a DRF router subclass, the addresses it generates are
    #: not certainly DRF's.
    rewrites_routes: bool = False


@dataclass
class FileViews:
    functions: dict[str, FunctionView] = field(default_factory=dict)
    classes: dict[str, ClassView] = field(default_factory=dict)


def django_views(index: Any | None) -> dict[str, FileViews] | None:
    found: dict[str, FileViews] | None = index_fact(index, FACT)
    return found


# ── collect: one file ─────────────────────────────────────────────────────────────────────


def _guard(expr: Node, source: bytes, origins: Origins) -> Guard | None:
    """A decorator as written without its arguments, and its origin."""
    if expr.type == "call":
        fn = expr.child_by_field_name("function")
        if fn is None:
            return None
        expr = fn
    if expr.type not in ("identifier", "attribute"):
        return None
    written = node_text(expr, source)
    return written, origins.qualified(written) or ""


def _decorators(decorated: Node, source: bytes, origins: Origins) -> list[tuple[Guard, Node]]:
    out = []
    for dec in (c for c in decorated.named_children if c.type == "decorator"):
        expr = dec.named_children[0] if dec.named_children else None
        guard = _guard(expr, source, origins) if expr is not None else None
        if expr is not None and guard is not None:
            out.append((guard, expr))
    return out


def _unwrap_method_decorator(guard: Guard, expr: Node, source: bytes,
                             origins: Origins) -> list[Guard]:
    """``method_decorator(login_required)`` / ``method_decorator([a, b])`` → the decorators it
    applies; any other decorator → itself."""
    if short(guard[0]) != "method_decorator" or expr.type != "call":
        return [guard]
    args = positional(expr)
    inner = args[0] if args else None
    items = inner.named_children if inner is not None and inner.type in ("list", "tuple") else [inner]
    return [g for item in items if item is not None and (g := _guard(item, source, origins)) is not None]


def _methods_of(decorators: list[tuple[Guard, Node]], source: bytes) -> tuple[str, ...] | None:
    """The verbs a ``require_*`` decorator allows (Django's own, by origin)."""
    for (written, origin), expr in decorators:
        name = short(written)
        if name not in METHOD_DECORATORS or not origin.startswith("django.views.decorators"):
            continue
        fixed = METHOD_DECORATORS[name]
        if fixed is not None:
            return fixed
        args = positional(expr) if expr.type == "call" else []
        values = string_list(args[0], source) if args else None
        return tuple(v.upper() for v in values) if values else None
    return None


def _names(node: Node | None, source: bytes, origins: Origins) -> tuple[Guard, ...] | None:
    """A literal list / tuple of class names (``[IsAuthenticated, perms.IsOwner]``) as guards;
    None when it is anything else."""
    if node is None or node.type not in ("list", "tuple"):
        return None
    out: list[Guard] = []
    for item in node.named_children:
        if item.type not in ("identifier", "attribute"):
            return None
        written = node_text(item, source)
        out.append((written, origins.qualified(written) or ""))
    return tuple(out)


def _api_view(decorators: list[tuple[Guard, Node]], source: bytes) -> tuple[str, ...] | None:
    for (written, origin), expr in decorators:
        if short(written) != "api_view" or not origin.startswith("rest_framework"):
            continue
        args = positional(expr) if expr.type == "call" else []
        value = args[0] if args else (keyword(expr, source, "http_method_names") if expr.type == "call" else None)
        values = string_list(value, source) if value is not None else None
        return tuple(dict.fromkeys(v.upper() for v in values)) if values else ("GET",)
    return None


def _permission_decorator(decorators: list[tuple[Guard, Node]], source: bytes,
                          origins: Origins) -> tuple[Guard, ...] | None:
    for (written, origin), expr in decorators:
        if short(written) == "permission_classes" and origin.startswith("rest_framework") \
                and expr.type == "call" and positional(expr):
            return _names(positional(expr)[0], source, origins)
    return None


def _action(decorators: list[tuple[Guard, Node]], source: bytes
            ) -> tuple[bool | None, tuple[str, ...], str | None] | None:
    """A DRF ``@action(…)``: (detail, verbs, url path)."""
    for (written, origin), expr in decorators:
        if short(written) != "action" or not origin.startswith("rest_framework"):
            continue
        detail_node = keyword(expr, source, "detail") if expr.type == "call" else None
        detail = {"True": True, "False": False}.get(node_text(detail_node, source)) \
            if detail_node is not None else None
        methods_node = keyword(expr, source, "methods") if expr.type == "call" else None
        methods = string_list(methods_node, source) if methods_node is not None else ["get"]
        url_node = keyword(expr, source, "url_path") if expr.type == "call" else None
        url_path = literal(url_node, source) if url_node is not None else None
        verbs = tuple(dict.fromkeys(m.upper() for m in methods)) if methods is not None else ()
        if url_node is not None and url_path is None:
            detail = None  # a path that is not a literal: no address
        return detail, verbs, url_path
    return None


def _class_attributes(view: "ClassView", body: Node, source: bytes, origins: Origins) -> None:
    for member in body.named_children:
        assign = member.named_children[0] if member.type == "expression_statement" and member.named_children else member
        left = assign.child_by_field_name("left") if assign.type == "assignment" else None
        if left is None or left.type != "identifier":
            continue
        name, right = node_text(left, source), assign.child_by_field_name("right")
        if name == "permission_classes":
            view.permissions = _names(right, source, origins)
            view.permissions_unknown = view.permissions is None
        elif name == "serializer_class" and right is not None and right.type in ("identifier", "attribute"):
            view.serializer = node_text(right, source)
        elif name in ("lookup_field", "lookup_url_kwarg"):
            setattr(view, name, literal(right, source))


#: Bases from these packages are never repo classes: no import needs resolving for them.
_LIBRARIES = ("django.", "rest_framework.")


def _base(node: Node, source: bytes, rel: str, local: set[str], imports: ImportRefs,
          origins: Origins) -> Base:
    written = node_text(node, source)
    origin = origins.qualified(written)
    if origin is not None and origin.startswith(_LIBRARIES):
        return ("lib", origin, written)
    if node.type == "identifier":
        if written in local:
            return ("repo", rel, written)
        ref = imports.get(written)
        if ref is not None and ref[0] == "symbol":
            return ("repo", ref[1], ref[2])
    elif node.type == "attribute":
        obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
        ref = imports.get(node_text(obj, source)) if obj is not None and obj.type == "identifier" else None
        if ref is not None and ref[0] == "module" and attr is not None:
            return ("repo", ref[1], node_text(attr, source))
    if origin is not None and not origin.startswith("."):
        return ("lib", origin, written)
    return ("unknown", written)


def _class(cdef: Node, decorated: Node | None, source: bytes, rel: str, local: set[str],
           imports: ImportRefs, origins: Origins) -> ClassView:
    view = ClassView(line=cdef.start_point[0] + 1)
    supers = cdef.child_by_field_name("superclasses")
    view.bases = tuple(_base(b, source, rel, local, imports, origins)
                       for b in (supers.named_children if supers is not None else [])
                       if b.type in ("identifier", "attribute"))
    dispatch: list[Guard] = []
    for guard, expr in _decorators(decorated, source, origins) if decorated is not None else []:
        name = keyword(expr, source, "name") if expr.type == "call" else None
        if short(guard[0]) == "method_decorator" and literal(name, source) == "dispatch":
            dispatch.extend(_unwrap_method_decorator(guard, expr, source, origins))
    view.dispatch_decorators = tuple(dispatch)
    body = cdef.child_by_field_name("body")
    for member in body.named_children if body is not None else []:
        fdef = member.child_by_field_name("definition") if member.type == "decorated_definition" else member
        if fdef is None or fdef.type != "function_definition":
            continue
        raw = _decorators(member, source, origins) if member.type == "decorated_definition" else []
        decorators = [g for guard, expr in raw for g in _unwrap_method_decorator(guard, expr, source, origins)]
        view.methods[field_text(fdef, "name", source)] = Method(
            fdef.start_point[0] + 1, tuple(decorators), _action(raw, source))
    if body is not None:
        _class_attributes(view, body, source, origins)
        text = source[body.start_byte:body.end_byte]
        view.rewrites_routes = any(m in text for m in (b"routes", b"trailing_slash", b"__init__(")) \
            and _rewrites_routes(body, source)
    return view


def _rewrites_routes(body: Node, source: bytes) -> bool:
    """Whether a class body assigns ``routes`` / ``self.routes`` or names ``trailing_slash``."""
    stack = list(body.named_children)
    while stack:
        node = stack.pop()
        if node.type in ("identifier", "attribute") and node_text(node, source).endswith("trailing_slash"):
            return True
        if node.type in ("assignment", "augmented_assignment"):
            left = node.child_by_field_name("left")
            if left is not None and node_text(left, source) in ("routes", "self.routes"):
                return True
        if node.type == "call" and _positional_to_base_init(node, source):
            return True
        stack.extend(node.named_children)
    return False


def _positional_to_base_init(call: Node, source: bytes) -> bool:
    """``super().__init__(x)`` / ``Base.__init__(self, x)``: a positional argument other than
    ``*args``."""
    written = field_text(call, "function", source)
    if not written.endswith(".__init__"):
        return False
    args = [a for a in positional(call) if a.type not in ("list_splat", "dictionary_splat")]
    return len(args) > (0 if written == "super().__init__" else 1)


def file_views(root: Node, source: bytes, rel: str, repo_root: Path | None) -> FileViews:
    """The module-level functions and classes of one file (see :class:`FileViews`)."""
    origins = Origins(root, source)
    imports = ImportRefs(root, source, repo_root, rel)  # resolved only for the bases that need it
    defs: list[tuple[Node, Node | None]] = []
    for stmt in root.named_children:
        inner = stmt.child_by_field_name("definition") if stmt.type == "decorated_definition" else stmt
        if inner is not None and inner.type in ("function_definition", "class_definition"):
            defs.append((inner, stmt if stmt.type == "decorated_definition" else None))
    local = {field_text(d, "name", source) for d, _ in defs if d.type == "class_definition"}
    out = FileViews()
    for inner, decorated in defs:
        name = field_text(inner, "name", source)
        if inner.type == "class_definition":
            out.classes[name] = _class(inner, decorated, source, rel, local, imports, origins)
            continue
        decorators = _decorators(decorated, source, origins) if decorated is not None else []
        out.functions[name] = FunctionView(
            line=inner.start_point[0] + 1,
            decorators=tuple(g for guard, expr in decorators
                             for g in _unwrap_method_decorator(guard, expr, source, origins)),
            methods=_methods_of(decorators, source),
            api_view=_api_view(decorators, source),
            permissions=_permission_decorator(decorators, source, origins),
        )
    return out


# ── resolve: a view's methods ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Handler:
    """The code that handles one verb: a method in the repo, or — ``inherited`` — a method a
    framework base provides, located at the view class."""

    name: str
    file: str
    line: int
    decorators: tuple[Guard, ...] = ()
    inherited: bool = False


@dataclass
class ResolvedClass:
    name: str
    file: str
    line: int
    #: verb (``GET`` …, ``ANY``) → its handler; empty when none is known.
    verbs: dict[str, Handler] = field(default_factory=dict)
    #: Guards on every method: auth mixins and ``dispatch`` decorators, outermost first.
    decorators: tuple[Guard, ...] = ()
    #: The class's bases, in method resolution order (repo classes as ``("repo", file, name)``).
    mro: tuple[Base, ...] = ()
    #: A DRF view (a base from ``rest_framework``).
    drf: bool = False
    #: A DRF viewset: routed by action, not by verb.
    viewset: bool = False
    #: Viewset action (``list``, …) → its handler.
    actions: dict[str, Handler] = field(default_factory=dict)
    #: ``@action`` methods: name → (handler, detail, verbs, url path).
    extra: dict[str, tuple[Handler, bool | None, tuple[str, ...], str | None]] = field(default_factory=dict)
    #: ``permission_classes`` of the class or the nearest base that sets it; None when none
    #: does (DRF's default applies) — ``()`` when one sets something that is not a literal list.
    permissions: tuple[Guard, ...] | None = None
    #: ``serializer_class``; None when not set or when ``get_serializer_class`` chooses it.
    serializer: str | None = None
    lookup: str = "pk"


def linearize(views: Mapping[str, FileViews], key: tuple[str, str]) -> list[Base]:
    """The class and its bases in method resolution order (depth first, left to right, each
    once — Python's C3 order for the single and mixin inheritance views use)."""
    out: list[Base] = []
    seen: set[Base] = set()

    def visit(base: Base) -> None:
        if base in seen:
            return
        seen.add(base)
        out.append(base)
        if base[0] == "repo":
            cls = views.get(base[1], FileViews()).classes.get(base[2])
            for parent in cls.bases if cls is not None else ():
                visit(parent)

    visit(("repo", *key))
    return out


def is_django_view(origin: str) -> bool:
    return origin.startswith(DJANGO_VIEW_MODULES)


def resolve_class(views: Mapping[str, FileViews], file: str, name: str) -> ResolvedClass | None:
    """The verbs a class-based view handles and who handles each, or None when the class is not
    in ``views``."""
    cls = views.get(file, FileViews()).classes.get(name)
    if cls is None:
        return None
    out = ResolvedClass(name=name, file=file, line=cls.line, mro=tuple(linearize(views, (file, name))))
    decorators: list[Guard] = []
    dispatch: Handler | None = None
    for base in out.mro:
        if base[0] == "repo":
            found = views.get(base[1], FileViews()).classes.get(base[2])
            if found is None:
                continue
            decorators.extend(found.dispatch_decorators)
            for method, info in found.methods.items():
                if method.upper() in VERBS and method.upper() not in out.verbs:
                    out.verbs[method.upper()] = Handler(method, base[1], info.line, info.decorators)
                elif method == "dispatch" and dispatch is None:
                    dispatch = Handler(method, base[1], info.line, info.decorators)
        elif base[0] == "lib" and base[1].startswith(AUTH_MIXIN_MODULE) and short(base[1]) in AUTH_MIXINS:
            decorators.append((base[2], base[1]))
        elif base[0] == "lib" and is_django_view(base[1]):
            for verb in DJANGO_VIEWS.get(short(base[1]), ()):
                if verb not in out.verbs:
                    out.verbs[verb] = Handler("get" if verb == "ANY" else verb.lower(), file, cls.line,
                                              inherited=True)
        elif base[0] == "lib" and base[1].startswith(drf.MODULE):
            _drf_base(views, out, short(base[1]))
    if out.drf:
        _drf_class(views, out)
    if "ANY" in out.verbs:  # RedirectView: every verb, whatever else is overridden
        out.verbs = {"ANY": out.verbs["ANY"]}
    if not out.verbs and dispatch is not None:
        out.verbs["ANY"] = dispatch
    out.decorators = tuple(decorators)
    return out


def _repo_classes(views: Mapping[str, FileViews], resolved: ResolvedClass) -> list[tuple[str, ClassView]]:
    out = []
    for base in resolved.mro:
        cls = views.get(base[1], FileViews()).classes.get(base[2]) if base[0] == "repo" else None
        if cls is not None:
            out.append((base[1], cls))
    return out


def _override(views: Mapping[str, FileViews], resolved: ResolvedClass, name: str) -> Handler | None:
    """The repo's own definition of method ``name``, nearest first."""
    for file, cls in _repo_classes(views, resolved):
        method = cls.methods.get(name)
        if method is not None:
            return Handler(name, file, method.line, method.decorators)
    return None


def _drf_base(views: Mapping[str, FileViews], out: ResolvedClass, name: str) -> None:
    """What a DRF base adds: a generic API view's verbs, a viewset's actions."""
    out.drf = True
    out.viewset = out.viewset or name in drf.VIEWSET_BASES
    for verb, action in drf.API_VIEWS.get(name, ()):
        if verb not in out.verbs:  # an overridden action (``def list``) handles it
            out.verbs[verb] = _override(views, out, action) or Handler(action, out.file, out.line, inherited=True)
    for action in drf.VIEWSET_ACTIONS.get(name, ()):
        if action not in out.actions:
            out.actions[action] = _override(views, out, action) or Handler(action, out.file, out.line,
                                                                           inherited=True)


def _drf_class(views: Mapping[str, FileViews], out: ResolvedClass) -> None:
    """DRF attributes, nearest class first: actions defined in the repo, ``@action`` methods,
    ``permission_classes``, ``serializer_class`` and the lookup."""
    classes = _repo_classes(views, out)
    lookup_field = lookup_kwarg = None
    for file, cls in classes:
        for name, method in cls.methods.items():
            if out.viewset and name in drf.ACTION_ROUTES and name not in out.actions:
                out.actions[name] = Handler(name, file, method.line, method.decorators)
            if method.action is not None and name not in out.extra:
                detail, verbs, url_path = method.action
                out.extra[name] = (Handler(name, file, method.line, method.decorators), detail, verbs, url_path)
        if out.permissions is None and (cls.permissions is not None or cls.permissions_unknown):
            out.permissions = cls.permissions or ()
        if out.serializer is None:
            out.serializer = cls.serializer
        lookup_field = lookup_field or cls.lookup_field
        lookup_kwarg = lookup_kwarg or cls.lookup_url_kwarg
    if any("get_serializer_class" in cls.methods for _, cls in classes):
        out.serializer = None
    out.lookup = lookup_kwarg or lookup_field or "pk"
    if out.viewset:  # a viewset answers by action: its verbs come from the router or as_view()
        out.verbs = {}


def standard_router(views: Mapping[str, FileViews], file: str, name: str) -> bool:
    """Whether the repo class ``name`` is a DRF router that generates DRF's standard routes: built
    on ``DefaultRouter`` / ``SimpleRouter`` through repo classes only, none of which assigns
    ``routes``, changes ``trailing_slash`` or overrides a method that builds the URLs
    (:data:`~.drf.ROUTER_URL_METHODS`). A subclass that only adds to the routes (a mapping on an
    existing route) still serves the standard ones."""
    found = False
    for base in linearize(views, (file, name)):
        if base[0] == "repo":
            cls = views.get(base[1], FileViews()).classes.get(base[2])
            if cls is None or cls.rewrites_routes or drf.ROUTER_URL_METHODS & cls.methods.keys():
                return False
        elif base[0] == "lib" and base[1] in drf.ROUTERS:
            found = True
        elif base != ("unknown", "object"):
            return False
    return found


def known_bases(resolved: ResolvedClass) -> bool:
    """Whether every base is in the repo or one of Django's views whose methods are known — so
    the verbs found are all the verbs the view handles."""
    for base in resolved.mro:
        if base[0] == "unknown" and base[1] != "object":
            return False
        if base[0] == "lib" and not (
            (is_django_view(base[1]) and (short(base[1]) in DJANGO_VIEWS or short(base[1]) in DJANGO_BASES))
            or (base[1].startswith(AUTH_MIXIN_MODULE))
        ):
            return False
    return True


def guards_of(decorators: tuple[Guard, ...] | list[Guard]) -> list[Guard]:
    """The decorators that are guards (not response shaping, method restriction, …)."""
    return [g for g in decorators if short(g[0]) not in NOT_GUARDS and g[0] not in NOT_GUARDS]


def certain(decorators: tuple[Guard, ...] | list[Guard]) -> bool:
    return any(origin in CERTAIN_AUTH for _, origin in decorators)


def login_not_required(decorators: tuple[Guard, ...] | list[Guard]) -> bool:
    return any(origin == LOGIN_NOT_REQUIRED for _, origin in decorators)


# ── the fact ─────────────────────────────────────────────────────────────────────────────


def _collect(root: Node, source: bytes, rel: str, repo_root: Path | None) -> tuple[str, FileViews] | None:
    views = file_views(root, source, rel, repo_root)
    return (rel, views) if views.functions or views.classes else None


def _resolve(values: list[tuple[str, FileViews]], index: Any) -> dict[str, FileViews] | None:
    return dict(values) or None


register_fact(FactSpec(
    # A view module need not import Django (``def orders(request)``, a subclass of a project base
    # view): function views take ``request``, class-based views have ``View`` in a base's name.
    # DRF router subclasses and Channels consumers are read too: ``Router`` / ``Consumer`` in a
    # class name.
    name=FACT, language="python", gate=(b"django", b"rest_framework", b"request", b"View", b"Router",
                                       b"Consumer"),
    collect=_collect, resolve=_resolve,
))
