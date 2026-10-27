"""chi routes.

::

    r := chi.NewRouter()
    r.Route("/articles", func(r chi.Router) {
        r.Get("/{id}", getArticle)    → route GET /articles/{id}, handler getArticle
    })
    r.Mount("/admin", adminRouter())  → the routes of ``adminRouter`` get /admin

``Group(fn)`` and ``With(mw…)`` keep the prefix; ``Handle`` / ``HandleFunc`` serve every
verb and are recorded with an empty ``method``; ``Method(verb, path, h)`` takes the verb as an
argument."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..go.routing import RouterSpec, detect_routes, register_router_fact

SPEC = RouterSpec(
    name="chi",
    import_path="github.com/go-chi/chi",
    router_types=frozenset({"Router", "Mux", "Routes"}),
    app_types=frozenset({"Mux"}),
    constructors=frozenset({"NewRouter", "NewMux"}),
    verbs={"Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH", "Delete": "DELETE",
           "Options": "OPTIONS", "Head": "HEAD", "Handle": None, "HandleFunc": None},
    verb_arg={"Method": 1, "MethodFunc": 1},
    groups={"Route": "closure_child", "Group": "closure_same", "With": "with", "Mount": "mount"},
    use=frozenset({"Use"}),
)

register_router_fact(SPEC)


def _detect(dc: DetectContext) -> Detection | None:
    return detect_routes(dc, SPEC)


register_detector(Detector(
    name="go-chi", gate=Gate("go", order=40, guard=b"go-chi/chi", skip_fixtures=True),
    detect=_detect, frameworks=(SPEC.name,),
))
