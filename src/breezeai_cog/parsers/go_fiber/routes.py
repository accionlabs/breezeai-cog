"""Fiber routes.

::

    app := fiber.New()
    api := app.Group("/api")
    api.Get("/users/:id", getUser)    → route GET /api/users/:id, handler getUser
    app.Route("/admin", func(r fiber.Router) { r.Post("/x", h) })  → route POST /admin/x

``All`` is every verb (``ANY``); ``Add(method, path, h)`` takes the verb as an argument;
``Static`` is framework-served (``GET``, no handler); ``Mount(prefix, sub)`` serves a sub-app
under the prefix."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..go.routing import RouterSpec, detect_routes, register_router_fact

SPEC = RouterSpec(
    name="fiber",
    import_path="github.com/gofiber/fiber",
    router_types=frozenset({"App", "Router", "Group"}),
    app_types=frozenset({"App"}),
    constructors=frozenset({"New"}),
    verbs={"Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH", "Delete": "DELETE",
           "Options": "OPTIONS", "Head": "HEAD", "All": "ANY"},
    verb_arg={"Add": 1},
    statics=frozenset({"Static"}),
    groups={"Group": "child", "Route": "closure_child", "Mount": "mount"},
    use=frozenset({"Use"}),
    group_guards=True,
    route_guards="before",
)

register_router_fact(SPEC)


def _detect(dc: DetectContext) -> Detection | None:
    return detect_routes(dc, SPEC)


register_detector(Detector(
    name="go-fiber", gate=Gate("go", order=50, guard=b"gofiber/fiber", skip_fixtures=True),
    detect=_detect, frameworks=(SPEC.name,),
))
