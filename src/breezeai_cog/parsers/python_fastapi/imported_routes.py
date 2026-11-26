"""Additive detector: FastAPI routes in a file that never mentions ``fastapi``.

A router is often made in one file and decorated in another that only imports it::

    # app/api/routing.py
    router = APIRouter(prefix="/orders")

    # app/api/orders.py — no "fastapi" in it, so FastAPIParser does not claim it
    from app.api.routing import router

    @router.get("/{id}")                      → route  GET  <served prefix>/orders/{id}
    def get_order(id: str): …

The routes are read exactly as :func:`.routes.detect_routes` reads them, with only imported
receivers: a decorator counts only when its receiver is imported from the file that makes the
router, found in the ``fastapi.wiring`` fact. A file that mentions ``fastapi`` is
FastAPIParser's, so it is skipped here — never two records for one route.
"""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from .routes import VERBS, detect_routes
from .wiring import fastapi_wiring


#: A route decorator calls one of these on its receiver; a file with none has no route.
_VERB_CALLS = tuple(f".{v}(".encode() for v in (*sorted(VERBS), "api_route"))


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    if b"fastapi" in source or not any(v in source for v in _VERB_CALLS):
        return None
    wiring = fastapi_wiring(dc.ctx.resolution_index)
    if wiring is None or not wiring.routers:
        return None
    routes = detect_routes(
        dc.root, source, dc.path, index=dc.ctx.resolution_index, repo_root=dc.ctx.repo_root
    )
    return Detection(routes, label="fastapi") if routes else None


# An entry-point emitter, so fixture/test files are skipped.
register_detector(
    Detector(
        name="fastapi-imported-routes",
        gate=Gate("python", order=70, skip_fixtures=True),  # guards itself: needs the wiring fact
        detect=_detect,
        frameworks=("fastapi",),
    )
)
