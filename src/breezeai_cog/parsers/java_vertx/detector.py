"""Vert.x routes, EventBus calls, timers and deployments as an **additive** Java detector.

Vert.x is a library, not a file kind: a Spring ``@Component`` that calls ``vertx.setPeriodic`` or a
JAX-RS resource that builds a ``Router`` holds Vert.x entry points too. One parser owns each file,
so the detection runs here — inside whichever Java parser owns it (``java-vertx``, Spring, JAX-RS,
plain Java) — on any file naming ``io.vertx`` (3.x+) or ``org.vertx`` (2.x). A file naming
neither is read only for its ``super(…)`` / ``this(…)`` calls, and only when the repo has a
constructor that registers its own class on the address it is given.

It runs last among the Java detectors and its file label outranks theirs, as the Vert.x pass did
when only the ``java-vertx`` parser ran it, so a Vert.x-only file's output is unchanged. A Spring
file keeps ``framework: spring`` when Spring finds its own entry points; each Vert.x record
carries ``framework: vertx`` itself.
"""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..constfold import resolve_all
from ..java.constants import collect_constants
from ..java.imports import JavaIndex
from .events import detect_vertx
from .forwarders import constructor_sites, has_constructor_sites


def _detect(dc: DetectContext) -> Detection | None:
    marked = b"io.vertx" in dc.source or b"org.vertx" in dc.source
    index = dc.ctx.resolution_index
    if not marked and not has_constructor_sites(dc.source, index):
        return None
    cross = index.consts if isinstance(index, JavaIndex) else {}  # repo-wide constants
    consts = resolve_all(collect_constants(dc.root, dc.source), base=cross)
    if marked:
        found = detect_vertx(dc.root, dc.source, dc.path, dc.record, consts, index)
    else:
        found = constructor_sites(dc.root, dc.source, dc.path, dc.record, consts, index)
    return Detection(found, label="vertx") if found else None


register_detector(Detector(
    name="vertx",
    # Two markers (3.x and 2.x) and the constructor check, so the detector guards itself. An
    # entry-point emitter, so fixture / test files are skipped.
    gate=Gate("java", order=90, guard=None, skip_fixtures=True),
    detect=_detect, frameworks=("vertx",),
    label_priority=1,  # a Vert.x file is labelled vertx whatever other library it uses
))
