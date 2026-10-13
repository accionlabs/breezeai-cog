"""Registry and runner for **additive detectors**.

An additive detector captures a cross-cutting library (messaging SDK, search index, vendor
SDK, a route style that can appear in any file) on top of whichever parser owns the file. It
never owns a file: the base language parser's ``extract`` calls :func:`run_additive` once, and
every detector registered for that language runs there, so it also fires inside every
framework parser that subclasses the language parser.

A detector registers itself from its own module::

    register_detector(Detector(
        name="kafka", language="csharp", order=30, run=_run,
        guard=b"Confluent.Kafka", skip_fixtures=True, frameworks=("kafka",),
    ))

No parser is edited to add one. Discovery finds modules under ``parsers/`` whose source
contains the ``register_detector(`` marker and imports them (:func:`discover_detectors`), so
a detector module does not have to be imported by anything else to be found.

The runner owns the shared rules: run only with statement capture, skip fixture files when
``skip_fixtures`` is set, apply the optional byte ``guard``, run in ascending ``order``, and
let a returned framework label set ``record.framework`` only while it is still ``None``.

Run order is part of the output: a detector disambiguates new statement ids against the
statements already on the record, so swapping two detectors can swap ids (the backend's
``captureId``). ``order`` is therefore explicit and unique per language.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tree_sitter import Node

from ..errors import RegistryError
from ..schemas import FileRecord
from .base import ParseContext

#: Languages whose base parser calls :func:`run_additive`. Registering a detector for any
#: other language is an error: nothing would ever run it.
HOOKED_LANGUAGES: frozenset[str] = frozenset({"typescript", "csharp", "scala"})

#: Source marker that makes a module a detector module for :func:`discover_detectors`.
_MARKER = b"register_detector("


@dataclass(slots=True)
class DetectContext:
    """Everything a detector gets for one file. Language-specific inputs are typed optional
    fields, filled by the language parser that has them."""

    root: Node
    ctx: ParseContext
    record: FileRecord
    is_fixture: bool
    #: TypeScript: import bindings (local name → resolved module) from ``extract_imports``.
    bindings: dict[str, str] | None = None
    #: Scala: local type map (identifier → declared type) from ``type_map``.
    types: dict[str, str] | None = None

    @property
    def source(self) -> bytes:
        return self.ctx.source

    @property
    def path(self) -> str:
        return self.ctx.path


@dataclass(frozen=True, slots=True)
class Detector:
    name: str
    language: str
    #: Ascending run order within the language; must be unique per language.
    order: int
    #: Enrich/append statements on ``dc.record``; return a file-level framework label or None.
    run: Callable[[DetectContext], str | None]
    #: Cheap byte check; the detector is skipped when the marker is absent. ``None`` = the
    #: detector guards itself (e.g. a structural check, or several markers).
    guard: bytes | None = None
    #: Skip route-only fixture files (stories, mocks). Set for entry-point/route emitters.
    skip_fixtures: bool = False
    #: Framework labels this detector can emit (statement or file level), for ``capabilities``.
    frameworks: tuple[str, ...] = ()


_DETECTORS: dict[str, list[Detector]] = {}
_discovered = False
_lock = threading.Lock()


def register_detector(detector: Detector) -> Detector:
    if detector.language not in HOOKED_LANGUAGES:
        raise RegistryError(
            f"detector {detector.name!r}: language {detector.language!r} has no additive "
            f"hook (one of {sorted(HOOKED_LANGUAGES)})"
        )
    existing = _DETECTORS.setdefault(detector.language, [])
    for other in existing:
        if other.name == detector.name:
            return other  # idempotent re-import
        if other.order == detector.order:
            raise RegistryError(
                f"detector {detector.name!r}: order {detector.order} already used by "
                f"{other.name!r} ({detector.language})"
            )
    existing.append(detector)
    existing.sort(key=lambda d: d.order)
    return detector


def discover_detectors() -> None:
    """Import every module under ``parsers/`` that registers a detector (idempotent).

    Runs lazily on first use in each process — worker processes are spawned, so each one
    discovers for itself."""
    global _discovered
    if _discovered:
        return
    with _lock:
        if _discovered:
            return
        root = Path(__file__).resolve().parent
        for py in sorted(root.rglob("*.py")):
            if py.name == "additive.py" or "__pycache__" in py.parts:
                continue
            if _MARKER not in py.read_bytes():
                continue
            rel = py.relative_to(root).with_suffix("")
            parts = [p for p in rel.parts if p != "__init__"]
            importlib.import_module(f"{__package__}.{'.'.join(parts)}")
        _discovered = True


def detectors_for(language: str) -> list[Detector]:
    """Registered detectors for ``language`` in run order."""
    discover_detectors()
    return list(_DETECTORS.get(language, ()))


def all_detectors() -> dict[str, list[Detector]]:
    discover_detectors()
    return {lang: list(ds) for lang, ds in sorted(_DETECTORS.items())}


def run_additive(
    language: str,
    root: Node,
    ctx: ParseContext,
    record: FileRecord,
    *,
    is_fixture: bool,
    bindings: dict[str, str] | None = None,
    types: dict[str, str] | None = None,
) -> None:
    """Run every additive detector registered for ``language`` on ``record``."""
    if not ctx.capture_statements:
        return
    dc = DetectContext(root, ctx, record, is_fixture, bindings=bindings, types=types)
    for detector in detectors_for(language):
        if detector.skip_fixtures and is_fixture:
            continue
        if detector.guard is not None and detector.guard not in ctx.source:
            continue
        label = detector.run(dc)
        if label and record.framework is None:
            record.framework = label
