"""Echo routes.

::

    e := echo.New()
    g := e.Group("/admin")
    g.POST("/users", createUser)      → route POST /admin/users, handler createUser

``Any`` is every verb (``ANY``); ``Add(method, path, h)`` takes the verb as an argument;
``Static`` / ``File`` are framework-served (``GET``, no handler). ``CONNECT`` / ``TRACE``
routes have no ``method`` value in the contract and are not recorded."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..go.routing import RouterSpec, detect_routes, register_router_fact

SPEC = RouterSpec(
    name="echo",
    import_path="github.com/labstack/echo",
    router_types=frozenset({"Echo", "Group"}),
    app_types=frozenset({"Echo"}),
    constructors=frozenset({"New"}),
    verbs={"GET": "GET", "POST": "POST", "PUT": "PUT", "PATCH": "PATCH", "DELETE": "DELETE",
           "OPTIONS": "OPTIONS", "HEAD": "HEAD", "Any": "ANY"},
    verb_arg={"Add": 1},
    statics=frozenset({"Static", "File"}),
    groups={"Group": "child"},
    adapters=frozenset({"WrapHandler"}),
    handler_after_path=True,
    verb_list={"Match": 1},
    use=frozenset({"Use", "Pre"}),
    group_guards=True,
    route_guards="after",
)

register_router_fact(SPEC)


def _detect(dc: DetectContext) -> Detection | None:
    return detect_routes(dc, SPEC)


register_detector(Detector(
    name="go-echo", gate=Gate("go", order=30, guard=b"labstack/echo", skip_fixtures=True),
    detect=_detect, frameworks=(SPEC.name,),
))
