"""Django route detection: one ``route`` record per binding in a urlconf's ``urlpatterns``
(role table: ``docs/entry-point-roles/django.md``).

The record sits on the ``path(…)`` call and leads to the view: a function (``ANY``, or the verbs
its ``require_*`` decorator allows) or a class-based view (one record per verb it handles, see
:func:`~.views.resolve_class`). The address is the urlconf's own (from the ``django.urlconf``
fact: the include chain from ``ROOT_URLCONF``) joined to the pattern; the view and its bases come
from the ``django.views`` fact when they are declared in another file. Without the repo index the
urlconf's address is unknown, so the records have no ``endpoint``.
"""

from __future__ import annotations

from collections import ChainMap
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node

from ..django_common import DJANGO_VIEWS
from ..emits import Emit
from ..handler_sites import handler_location
from ..python_common import Origins, attribute_owner, field_text, import_refs, literal, positional
from ..treesitter import node_text
from . import drf
from .generics import LOGIN_REQUIRED_MIDDLEWARE, short
from .patterns import Part, join
from .urlconf import django_urlconf, include_target
from .urlpatterns import Binding, Include, urlconf
from .views import (
    FileViews, FunctionView, Guard, Handler, ResolvedClass, certain, django_views, export_of, file_views,
    guards_of, is_django_view, login_not_required, resolve_class,
)

FRAMEWORK = "django"


@dataclass
class Route:
    """One record before it is written: what the view says about one verb."""

    method: str | None
    handler: str | None = None
    #: (file, line) of the handler's declaration; None when not located.
    site: tuple[str, int] | None = None
    guards: tuple[Guard, ...] = ()
    #: Every decorator of the view, for ``@login_not_required``.
    decorators: tuple[Guard, ...] = ()
    framework: str = FRAMEWORK
    #: DRF permission classes that apply (the view's, else the settings' default).
    permissions: tuple[Guard, ...] = ()
    request_dto: str | None = None
    response_dto: str | None = None


class UrlFile:
    """What every binding of one urlconf needs: its imports, the views it can name, the address
    it is served at and the settings every view runs under."""

    def __init__(self, root: Node, source: bytes, path: str, index: object | None,
                 repo_root: Path | None) -> None:
        self.root, self.source, self.path, self.repo_root = root, source, path, repo_root
        self.ctx_index = index
        self.origins = Origins(root, source)
        self.imports = import_refs(root, source, repo_root, path, plain=True) if repo_root is not None else {}
        # This file's own views over the repo's (never copied: it is read-only and repo-sized).
        self.views: Mapping[str, FileViews] = ChainMap(
            {path: file_views(root, source, path, repo_root)}, django_views(index) or {})
        wiring = django_urlconf(index)
        self.base: tuple[Part, ...] | None = wiring.served.get(path) if wiring is not None else None
        self.dotted: dict[str, str] = wiring.dotted if wiring is not None else {}
        self.middleware: tuple[str, ...] = wiring.settings.get("MIDDLEWARE", ()) if wiring is not None else ()
        #: DRF's default permission classes, when every settings module agrees.
        self.drf_permissions: tuple[Guard, ...] = tuple(
            (p, p) for p in (wiring.settings.get("DRF_PERMISSIONS", ()) if wiring is not None else ()))

    def target(self, node: Node) -> tuple[str, str] | None:
        """(file, name) of the module-level function or class ``node`` names: this file's, an
        imported one (``from .views import orders``) or an attribute of an imported module
        (``views.orders``)."""
        source = self.source
        if node.type == "identifier":
            name = node_text(node, source)
            if name in self.views[self.path].functions or name in self.views[self.path].classes:
                return self.path, name
            ref = self.imports.get(name)
            return export_of(self.views, ref[1], ref[2]) if ref is not None and ref[0] == "symbol" else None
        if node.type == "attribute":
            owner = attribute_owner(node, source, self.imports, self.repo_root)
            return export_of(self.views, *owner) if owner is not None else None
        return None

    def address(self, binding: Binding) -> tuple[str | None, bool | None]:
        if self.base is None or binding.prefix is None or binding.route is None:
            return None, None
        joined = join([*self.base, *binding.prefix, binding.route])
        return joined if joined is not None else (None, None)


# ── views → routes ───────────────────────────────────────────────────────────────────────


def function_routes(f: UrlFile, view: FunctionView, name: str, file: str) -> list[Route]:
    guards = tuple(guards_of(view.decorators))
    if view.api_view is not None:  # DRF @api_view: its verbs, DRF's permissions
        perms = view.permissions if view.permissions is not None else f.drf_permissions
        return [Route(m, name, (file, view.line), guards, view.decorators, drf.FRAMEWORK, perms)
                for m in view.api_view]
    return [Route(m, name, (file, view.line), guards, view.decorators)
            for m in (view.methods or ("ANY",))]


def handler_route(f: UrlFile, resolved: ResolvedClass, verb: str | None, handler: Handler) -> Route:
    """The record of one verb of a class-based view: its guards, and for DRF its permission
    classes and — on an action DRF provides — the serializer as the payload type."""
    guards = (*guards_of(resolved.decorators), *guards_of(handler.decorators))
    out = Route(verb, handler.name, (handler.file, handler.line), tuple(guards),
                (*resolved.decorators, *handler.decorators))
    if resolved.drf:
        out.framework = drf.FRAMEWORK
        out.permissions = resolved.permissions if resolved.permissions is not None else f.drf_permissions
        extra = resolved.extra.get(handler.name)
        if extra is not None and extra[0] == handler and handler.name in resolved.action_permissions:
            out.permissions = resolved.action_permissions[handler.name]  # an @action's replace the view's
        if handler.inherited and resolved.serializer is not None:
            out.request_dto = resolved.serializer if verb in drf.BODY_METHODS else None
            out.response_dto = resolved.serializer if verb not in drf.NO_RESPONSE_BODY else None
    return out


def class_routes(f: UrlFile, resolved: ResolvedClass) -> list[Route]:
    out = [handler_route(f, resolved, verb, handler) for verb, handler in resolved.verbs.items()]
    if not out:  # its methods are not visible (a base from another library): one record
        out.append(handler_route(f, resolved, None, Handler(resolved.name, resolved.file, resolved.line)))
    return out


def viewset_routes(f: UrlFile, resolved: ResolvedClass, mapping: Node) -> list[Route]:
    """``OrderViewSet.as_view({"get": "list", "post": "create"})``: one record per entry."""
    out = []
    for pair in mapping.named_children:
        verb = literal(pair.child_by_field_name("key"), f.source) if pair.type == "pair" else None
        action = literal(pair.child_by_field_name("value"), f.source) if pair.type == "pair" else None
        if verb is None or action is None:
            continue
        handler = resolved.actions.get(action) or (resolved.extra[action][0] if action in resolved.extra
                                                   else Handler(action, resolved.file, resolved.line))
        out.append(handler_route(f, resolved, verb.upper(), handler))
    return out


_PARTIAL = frozenset({"partial", "functools.partial"})
_MAX_WRAPPERS = 5


def _instance_routes(f: UrlFile, file: str, name: str) -> list[Route]:
    """``path("rss/", LatestFeed())``: Django calls the instance for every verb — ``__call__``
    where the repo defines it on the class or a base, else inherited (located at the class)."""
    resolved = resolve_class(f.views, file, name)
    if resolved is None:
        return []
    for base in resolved.mro:
        cls = f.views.get(base[1], FileViews()).classes.get(base[2]) if base[0] == "repo" else None
        call = cls.methods.get("__call__") if cls is not None else None
        if call is not None:
            return [Route("ANY", "__call__", (base[1], call.line), tuple(guards_of(call.decorators)),
                          call.decorators)]
    return [Route("ANY", name, (file, resolved.line))]


def _wrapped(f: UrlFile, view: Node, depth: int) -> list[Route]:
    """A view wrapped in a call in the urlconf: ``login_required(views.profile)``,
    ``cache_page(60)(views.sitemap)``, ``partial(views.error, code=400)``. The wrapped view is the
    handler; a decorator is one more guard, outermost."""
    source = f.source
    fn = view.child_by_field_name("function")
    args = positional(view)
    if fn is None or not args or depth >= _MAX_WRAPPERS:
        return []
    written = node_text(fn, source)
    inner = view_routes(f, args[0], depth + 1)
    if written in _PARTIAL or f.origins.qualified(written) == "functools.partial":
        return inner
    decorator_node = fn.child_by_field_name("function") if fn.type == "call" else fn
    if decorator_node is None or decorator_node.type not in ("identifier", "attribute"):
        return []
    decorator_written = node_text(decorator_node, source)
    guard = (decorator_written, f.origins.qualified(decorator_written) or "")
    return [Route(r.method, r.handler, r.site, (*guards_of([guard]), *r.guards), (guard, *r.decorators),
                  r.framework) for r in inner]


def view_routes(f: UrlFile, view: Node, depth: int = 0) -> list[Route]:
    """The records a pattern's view gives, before the address is applied."""
    source = f.source
    if view.type == "lambda":  # an anonymous view: its line only
        return [Route("ANY", None, (f.path, view.start_point[0] + 1))]
    if view.type == "call":
        fn = view.child_by_field_name("function")
        if fn is None:
            return []
        if fn.type != "attribute" or field_text(fn, "attribute", source) != "as_view":
            target = f.target(fn) if fn.type in ("identifier", "attribute") else None
            if target is not None and target[1] in f.views.get(target[0], FileViews()).classes:
                return _instance_routes(f, *target)
            return _wrapped(f, view, depth)
        cls = fn.child_by_field_name("object")
        if cls is None:
            return []
        found = f.target(cls)
        resolved = resolve_class(f.views, *found) if found is not None else None
        mapping = positional(view)[0] if positional(view) else None
        if resolved is not None and mapping is not None and mapping.type == "dictionary":
            return viewset_routes(f, resolved, mapping)
        if resolved is not None:
            return class_routes(f, resolved)
        origin = f.origins.qualified(node_text(cls, source)) or ""
        if is_django_view(origin):  # TemplateView.as_view(…): served by Django's own code
            verbs: tuple[str | None, ...] = DJANGO_VIEWS.get(short(origin), (None,))
            return [Route(m, short(origin)) for m in verbs]
        return [Route(None, short(node_text(cls, source)))]
    if view.type not in ("identifier", "attribute"):
        return []
    found = f.target(view)
    fview = f.views.get(found[0], FileViews()).functions.get(found[1]) if found is not None else None
    if found is not None and fview is not None:
        return function_routes(f, fview, found[1], found[0])
    if found is not None and f.views.get(found[0], FileViews()).classes.get(found[1]) is not None:
        return []  # a class passed without as_view(): not a view Django can call
    return [Route("ANY", short(node_text(view, source)))]


# ── records ──────────────────────────────────────────────────────────────────────────────


def record(f: UrlFile, call: Node, route: Route, endpoint: str | None, is_regex: bool | None) -> Emit:
    line, handler_path = handler_location(
        route.site, f.path)
    permissions = [p for p in route.permissions if p[1] != drf.ALLOW_ANY]
    guards = [*f.middleware, *(g for g, _ in route.guards), *(p for p, _ in permissions)]
    claim = certain(route.guards) or drf.permission_certain([o for _, o in permissions], route.method) or (
        LOGIN_REQUIRED_MIDDLEWARE in f.middleware and not login_not_required(route.decorators))
    return Emit(call, dict(
        semanticType="route", method=route.method, endpoint=endpoint, isRegex=is_regex,
        framework=route.framework, handler=route.handler, handlerLine=line, handlerPath=handler_path,
        guards=list(dict.fromkeys(guards)) or None, authRequired=True if claim else None,
        requestDTO=route.request_dto, responseDTO=route.response_dto, routeKind="route",
    ))


def _library_mount(f: UrlFile, inc: Include) -> str | None:
    """The handler of a mount of a library's URLs (``include("allauth.urls")``,
    ``admin.site.urls``) as written; None when it is the repo's own (wiring, no record)."""
    if include_target(inc.target, f.source, f.path, f.repo_root, f.imports) is not None:
        return None
    kind, value = inc.target
    if kind == "module" and isinstance(value, str):
        top = value.split(".", 1)[0]
        if value in f.dotted:  # an urlconf of the repo, in a folder on sys.path
            return None
        return value if f.repo_root is None or not (f.repo_root / top).exists() else None
    if isinstance(value, Node):
        written = node_text(value, f.source)
        origin = f.origins.qualified(written)
        if origin is not None and not origin.startswith(".") and f.imports.get(written.split(".", 1)[0]) is None:
            return written
    return None


def detect_urls(root: Node, source: bytes, path: str, *, index: object | None = None,
                repo_root: Path | None = None) -> list[Emit]:
    """The Django routes of one urlconf module, for :func:`~..emits.apply`."""
    if b"urlpatterns" not in source:
        return []
    found = urlconf(root, source, Origins(root, source))
    if found is None:
        return []
    f = UrlFile(root, source, path, index, repo_root)
    bindings, includes = found
    out: list[Emit] = []
    for binding in bindings:
        endpoint, is_regex = f.address(binding)
        out.extend(record(f, binding.call, r, endpoint, is_regex) for r in view_routes(f, binding.view))
    for inc in includes:
        handler = _library_mount(f, inc)
        if handler is None:
            continue
        joined = join([*f.base, *inc.prefix]) if f.base is not None and inc.prefix is not None else None
        out.append(Emit(inc.call, dict(
            semanticType="route", method="ANY", endpoint=joined[0] if joined else None,
            isRegex=joined[1] if joined else None, framework=FRAMEWORK, handler=handler,
            routeKind="mount",
        )))
    return out
