"""Gin routes.

::

    r := gin.Default()
    api := r.Group("/api")
    api.GET("/users/:id", h.Get)      → route GET /api/users/:id, handler h.Get
    users.Register(api.Group("/users"))  → the routes of ``Register`` get /api/users

``Any`` is every verb (``ANY``); ``Handle(method, path, h)`` takes the verb as an argument;
``Static`` / ``StaticFS`` / ``StaticFile`` are framework-served (``GET``, no handler)."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..go.routing import RouterSpec, detect_routes, register_router_fact

SPEC = RouterSpec(
    name="gin",
    import_path="github.com/gin-gonic/gin",
    router_types=frozenset({"Engine", "RouterGroup", "IRouter", "IRoutes"}),
    app_types=frozenset({"Engine"}),
    constructors=frozenset({"New", "Default"}),
    verbs={"GET": "GET", "POST": "POST", "PUT": "PUT", "PATCH": "PATCH", "DELETE": "DELETE",
           "OPTIONS": "OPTIONS", "HEAD": "HEAD", "Any": "ANY"},
    verb_arg={"Handle": 1},
    statics=frozenset({"Static", "StaticFS", "StaticFile"}),
    groups={"Group": "child", "Use": "same"},
    adapters=frozenset({"WrapF", "WrapH"}),
)

register_router_fact(SPEC)


def _detect(dc: DetectContext) -> Detection | None:
    return detect_routes(dc, SPEC)


register_detector(Detector(
    name="go-gin", gate=Gate("go", order=20, guard=b"gin-gonic/gin", skip_fixtures=True),
    detect=_detect, frameworks=(SPEC.name,),
))
