"""DRF router detection: one ``route`` record per action of each ``router.register(prefix,
ViewSet)`` (role table: ``docs/entry-point-roles/drf.md``).

The record sits on the ``register`` call. Its address is where the router is included (the
``django.urlconf`` fact) joined to the routes DRF's routers generate, written in the path syntax
the Django records use::

    router.register("orders", OrderViewSet)     # included at "api/"
    → GET /api/orders/  (list)   GET /api/orders/<pk>/  (retrieve)   …

The actions are the viewset's (:func:`~.views.resolve_class`): the ones it or a base in the repo
defines, the ones its DRF bases provide, and its ``@action`` methods.

It is an additive detector, not part of the DRF parser: ``router.register(…)`` often sits in a
file that never imports DRF (``from shop.routers import router``), which no parser could claim.
It records a route only on a router the code shows to be DRF's (``DefaultRouter()`` /
``SimpleRouter()``, here or imported), so ``admin.site.register(…)`` is never one — or made from
a repo subclass of them that keeps DRF's routes (:func:`~.views.standard_router`: it may add to
them, never replace them).
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..python_common import Origins, argument, calls, field_text, literal
from . import drf
from .patterns import Part, as_path, join
from .urlconf import django_urlconf, local_routers, router_ref
from .urls import Route, UrlFile, handler_route, record
from .views import ResolvedClass, resolve_class, standard_router


def _routes(f: UrlFile, resolved: ResolvedClass, prefix: str | None, slash: str
            ) -> list[tuple[Route, str | None]]:
    """(record, path under the router) for every action of the viewset."""
    lookup = f"<{resolved.lookup}>"
    out = []
    for action, handler in resolved.actions.items():
        verb, detail = drf.ACTION_ROUTES[action]
        path = None if prefix is None else (f"{prefix}/{lookup}{slash}" if detail else f"{prefix}{slash}")
        out.append((handler_route(f, resolved, verb, handler), _bare(prefix, path)))
    for name, (method, on_detail, verbs, url_path) in resolved.extra.items():
        segment = url_path if url_path is not None else name
        extra_path = None if prefix is None or on_detail is None else (
            f"{prefix}/{lookup}/{segment}{slash}" if on_detail else f"{prefix}/{segment}{slash}")
        methods: tuple[str | None, ...] = verbs or (None,)
        for extra_verb in methods:
            out.append((handler_route(f, resolved, extra_verb, method), _bare(prefix, extra_path)))
    return out


def _bare(prefix: str | None, path: str | None) -> str | None:
    """``path`` as DRF serves it: with an empty prefix the generated route starts with ``/``,
    which DRF removes (``^/$`` → ``^$``), so ``api/`` + ``""`` is ``/api/``, not ``/api//``."""
    return path[1:] if prefix == "" and path is not None and path.startswith("/") else path


def detect_routers(root: Node, source: bytes, path: str, f: UrlFile) -> list[Emit]:
    """The DRF routes of the ``router.register(…)`` calls of one file, for
    :func:`~..emits.apply`."""
    wiring = django_urlconf(f.ctx_index)
    local = local_routers(root, source, f.origins, path, f.imports)
    out: list[Emit] = []
    for call in calls(root):
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "attribute" or field_text(fn, "attribute", source) != "register":
            continue
        ref = router_ref(fn.child_by_field_name("object"), source, path, local, f.imports, f.repo_root)
        served = wiring.routers.get(ref[1], {}).get(ref[2]) if wiring is not None and ref is not None else None
        made = served[2] if served is not None else local[ref[2]][1] if ref is not None and ref[2] in local \
            and ref[1] == path else None
        if ref is None or (served is None and (ref[1] != path or ref[2] not in local)):
            continue  # not a DRF router the code shows
        if made is not None and not standard_router(f.views, *made):
            continue  # a router class of the repo's own that changes the routes DRF generates
        viewset = argument(call, source, 1, "viewset")
        target = f.target(viewset) if viewset is not None else None
        resolved = resolve_class(f.views, *target) if target is not None else None
        if resolved is None or not resolved.viewset:
            continue
        prefix = literal(argument(call, source, 0, "prefix"), source)
        slash = "/" if served is None or served[1] else ""
        base: tuple[Part, ...] | None = served[0] if served is not None else None
        if prefix is not None and as_path(prefix) is None:
            prefix = None  # a regex prefix: not written in path syntax
        for route, sub in _routes(f, resolved, prefix, slash):
            joined = join([*base, (sub, False)]) if base is not None and sub is not None else None
            out.append(record(f, call, route, joined[0] if joined else None, joined[1] if joined else None))
    return out


#: Libraries whose ``register`` (``admin.site.register``, signal and template registries) is never
#: a DRF router's — skipped before any import is resolved.
_NOT_ROUTERS = ("django.", "rest_framework.")


def _candidate(dc: DetectContext) -> bool:
    """Whether a ``.register(`` call's receiver may be a router: not one of Django's own objects."""
    origins = Origins(dc.root, dc.source)
    for call in calls(dc.root):
        fn = call.child_by_field_name("function")
        if fn is None or fn.type != "attribute" or field_text(fn, "attribute", dc.source) != "register":
            continue
        head = field_text(fn, "object", dc.source).split(".", 1)[0]
        if not (origins.qualified(head) or "").startswith(_NOT_ROUTERS):
            return True
    return False


def _detect(dc: DetectContext) -> Detection | None:
    if not _candidate(dc):
        return None
    repo_root = Path(dc.ctx.repo_root) if dc.ctx.repo_root is not None else None
    f = UrlFile(dc.root, dc.source, dc.path, dc.ctx.resolution_index, repo_root)
    found = detect_routers(dc.root, dc.source, dc.path, f)
    return Detection(found, label=drf.FRAMEWORK) if found else None


register_detector(Detector(
    name="drf-routers",
    gate=Gate("python", order=30, guard=b".register(", skip_fixtures=True),
    detect=_detect,
    frameworks=(drf.FRAMEWORK,),
))
