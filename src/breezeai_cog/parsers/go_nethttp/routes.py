"""``net/http`` routes: ``ServeMux`` and the package's default mux.

::

    mux := http.NewServeMux()
    mux.HandleFunc("GET /items/{id}", getItem)   → route GET /items/{id}, handler getItem
    http.Handle("/static/", files)               → route (no method), /static/

A Go 1.22 pattern's verb goes to ``method``; a host (``example.com/x``) is not part of the
path. A pattern with no verb serves every verb and is recorded with an empty ``method``."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..contract import HTTP_METHODS
from ..go.routing import RouterSpec, detect_routes, register_router_fact


def _pattern(verb: str | None, path: str | None) -> tuple[str | None, str | None]:
    if path is None:
        return verb, None
    head, _, rest = path.strip().partition(" ")
    if rest and head in HTTP_METHODS:
        verb, path = head, rest.strip()
    if not path.startswith("/"):
        path = path[path.index("/"):] if "/" in path else None  # drop the host
    return verb, path


SPEC = RouterSpec(
    name="net-http",
    import_path="net/http",
    router_types=frozenset({"ServeMux"}),
    app_types=frozenset({"ServeMux"}),
    constructors=frozenset({"NewServeMux"}),
    verbs={"Handle": None, "HandleFunc": None},
    package_routes=True,
    adapters=frozenset({"HandlerFunc"}),
    strip_prefix=frozenset({"StripPrefix"}),
    pattern=_pattern,
)

register_router_fact(SPEC)


def _detect(dc: DetectContext) -> Detection | None:
    return detect_routes(dc, SPEC)


register_detector(Detector(
    name="go-net-http", gate=Gate("go", order=10, guard=b'"net/http"', skip_fixtures=True),
    detect=_detect, frameworks=(SPEC.name,),
))
