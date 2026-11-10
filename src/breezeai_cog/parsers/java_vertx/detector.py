"""Vert.x routes, EventBus calls, timers and deployments as an **additive** Java detector.

Vert.x is a library, not a file kind: a Spring ``@Component`` that calls ``vertx.setPeriodic`` or a
JAX-RS resource that builds a ``Router`` holds Vert.x entry points too. One parser owns each file,
so the detection runs here — inside whichever Java parser owns it (``java-vertx``, Spring, JAX-RS,
plain Java) — on any file naming ``io.vertx`` (3.x+) or ``org.vertx`` (2.x).

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


def _detect(dc: DetectContext) -> Detection | None:
    if b"io.vertx" not in dc.source and b"org.vertx" not in dc.source:
        return None
    index = dc.ctx.resolution_index
    cross = index.consts if isinstance(index, JavaIndex) else {}  # repo-wide constants
    consts = resolve_all(collect_constants(dc.root, dc.source), base=cross)
    found = detect_vertx(dc.root, dc.source, dc.path, dc.record, consts, index)
    return Detection(found, label="vertx") if found else None


register_detector(Detector(
    name="vertx",
    # Two markers (3.x and 2.x), so the detector guards itself. An entry-point emitter, so
    # fixture / test files are skipped.
    gate=Gate("java", order=90, guard=None, skip_fixtures=True),
    detect=_detect, frameworks=("vertx",),
    label_priority=1,  # a Vert.x file is labelled vertx whatever other library it uses
))
