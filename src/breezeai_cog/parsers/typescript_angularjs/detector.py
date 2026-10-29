"""The ``angularjs`` additive detector: one walk of the file, then each kind of construct reads
what it needs from it. Registered here so the modules it uses can import one another's helpers
without a cycle."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..handler_sites import handler_location
from ..typescript.handlers import handler_locator
from .registrations import Env, Variable, locator, registration_emits, registrations
from .resources import resource_emits
from .routes import ngroute_emits
from .states import declared_states, state_emits, state_lookup
from .syntax import FRAMEWORK, Scan, scan

#: A file mentioning none of these cannot hold a construct this detector reads.
_MARKERS = (
    b"$routeProvider",
    b"$stateProvider",
    b"$urlRouterProvider",
    b"$resource",
    b".module(",
)


def _variable_locator(dc: DetectContext, found: Scan) -> Variable:
    """Where a controller written as a variable is declared: a function, class or object of this
    file, else as any TypeScript handler is found (an import, followed to its module — and named
    as that module declares it). The TypeScript lookup reads the tree already parsed and is built
    only when a route needs it."""
    lookup = None

    def locate(name: str) -> tuple[str, int | None, str | None]:
        nonlocal lookup
        line = found.declared.get(name)
        if line is not None:
            return name, line, None
        if lookup is None:
            lookup = handler_locator(
                dc.root, dc.source, dc.path, dc.ctx.repo_root, dc.ctx.resolution_index
            )
        hit = lookup(name)
        if hit is None:
            return name, None, None
        line, path = handler_location(hit[1], dc.path)
        return hit[0] or name, line, path

    return locate


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    if not any(marker in source for marker in _MARKERS):
        return None
    found = scan(dc.root, source)
    here = registrations(found, source, dc.root)
    env = Env(source, dc.path, locator(dc, here), _variable_locator(dc, found))
    states = declared_states(found, source)
    emits = [
        *ngroute_emits(found, env),
        *state_emits(states, found, env, state_lookup(dc, states)),
        *registration_emits(here, source),
        *resource_emits(found, source),
    ]
    emits.sort(key=lambda e: e.node.start_byte)
    return Detection(emits, label=FRAMEWORK) if emits else None


register_detector(
    Detector(
        name="angularjs",
        gate=Gate("typescript", order=80, skip_fixtures=True),
        detect=_detect,
        frameworks=(FRAMEWORK,),
    )
)
