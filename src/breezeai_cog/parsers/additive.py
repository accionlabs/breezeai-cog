"""Registry and runner for **additive detectors** and the **facts** they read.

An additive detector captures a cross-cutting library (messaging SDK, search index, vendor
SDK, a route style that can appear in any file) on top of whichever parser owns the file. It
never owns a file: the base language parser's ``extract`` calls :func:`run_additive` once, and
every detector registered for that language runs there, so it also fires inside every
framework parser that subclasses the language parser.

A detector registers itself from its own module::

    register_detector(Detector(
        name="kafka",
        gate=Gate("csharp", order=30, guard=b"Confluent.Kafka", skip_fixtures=True),
        detect=_detect, frameworks=("kafka",),
    ))

No parser is edited to add one. Discovery finds modules under ``parsers/`` whose source
contains a registration marker and imports them (:func:`discover_detectors`), so a detector
module does not have to be imported by anything else to be found.

**When it runs** is the detector's :class:`Gate` and nothing else: only with statement capture,
not on fixture files when ``skip_fixtures`` is set, only when the byte ``guard`` is present, in
ascending ``order``.

**What it produces.** ``detect`` returns a :class:`Detection`: the statements it found as
:class:`~.emits.Emit` values and an optional file-level framework label. The runner applies the
emits with :func:`~.emits.apply`, which owns the shared output rules (enrich in place or append,
statement ids, parents), and sets the label only while ``record.framework`` is still ``None``.
A detector never edits the record itself.

**Facts from other files.** Some detections need a fact that lives in *another* file: a class
is a GraphQL root only because the composition root registers it (``AddQueryType<BookQueries>()``).
A :class:`FactSpec` declares one. The language's ``build_index`` calls :func:`collect_additive`
for every file (behind the fact's ``gate``, with the tree it already parsed) and
:func:`resolve_additive` once after the reduce. The result is stored on the language index under
the fact's name (``framework.concern``), and a detector or a parser reads it back with
:func:`index_fact`. A fact is built once before parsing and is read-only afterwards, like every
other index entry.

Run order is part of the output: new statement ids are disambiguated against the statements
already on the record, so swapping two detectors can swap ids (the backend's ``captureId``).
``order`` is therefore explicit and unique per language.
"""

from __future__ import annotations

import importlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tree_sitter import Node

from ..errors import RegistryError
from ..schemas import FileRecord
from .base import ParseContext
from .emits import Emit, apply

#: Languages whose base parser calls :func:`run_additive`. Registering a detector for any
#: other language is an error: nothing would ever run it.
HOOKED_LANGUAGES: frozenset[str] = frozenset({"typescript", "csharp", "java", "python", "scala", "php", "go"})

#: Languages whose ``build_index`` calls :func:`collect_additive` / :func:`resolve_additive`.
#: A fact for any other language is an error: it would never be built, and the detector
#: reading it would silently find nothing.
INDEX_HOOKED_LANGUAGES: frozenset[str] = frozenset(
    {"apex", "csharp", "go", "graphql", "groovy", "java", "php", "python", "ruby", "scala",
     "typescript"})

#: Source markers that make a module a registering module for :func:`discover_detectors`.
_MARKERS = (b"register_detector(", b"register_fact(")


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
class Gate:
    """When a detector runs. The registry evaluates it; the detector never re-checks it."""

    language: str
    #: Ascending run order within the language; must be unique per language.
    order: int
    #: Cheap byte check; the detector is skipped when the marker is absent. ``None`` = the
    #: detector guards itself (e.g. a structural check, or several markers).
    guard: bytes | None = None
    #: Skip route-only fixture files (stories, mocks). Set for entry-point/route emitters.
    skip_fixtures: bool = False

    def admits(self, source: bytes, *, is_fixture: bool) -> bool:
        """Would the detector run on a file with this source?"""
        if self.skip_fixtures and is_fixture:
            return False
        return self.guard is None or self.guard in source


@dataclass(slots=True)
class Detection:
    """What a detector found in one file."""

    emits: list[Emit]
    #: File-level framework label; set on the record only while it has none.
    label: str | None = None


@dataclass(frozen=True, slots=True)
class Detector:
    name: str
    gate: Gate
    #: Find the file's statements; the runner applies them. ``None`` = nothing found.
    detect: Callable[[DetectContext], Detection | None]
    #: Framework labels this detector can emit (statement or file level), for ``capabilities``.
    frameworks: tuple[str, ...] = ()

    @property
    def language(self) -> str:
        return self.gate.language

    @property
    def order(self) -> int:
        return self.gate.order


@dataclass(frozen=True, slots=True)
class FactSpec:
    """A fact built from many files while the language index is built.

    ``collect(root, source, rel, repo_root)`` runs during the language's ``build_index`` on
    files containing one of ``gate`` and returns a picklable value (or None). ``resolve(values,
    index)`` runs once after every file is indexed, gets the values in file order, must not
    depend on that order, and returns the fact read back with :func:`index_fact`."""

    #: ``framework.concern``, e.g. ``hotchocolate.registered-roots``; unique per language.
    name: str
    language: str
    gate: tuple[bytes, ...]
    collect: Callable[[Any, bytes, str, Path | None], Any]  # root: a Node, or None (no grammar)
    resolve: Callable[[list[Any], Any], Any]
    #: Extra file extensions to collect from, owned by another parser (``(".svc",)`` for a C#
    #: fact, ``(".graphql",)`` for a TypeScript one). The pipeline hands such files to this
    #: language's index too; they reach only this ``collect``, never the language's own
    #: indexing, and the owning parser still emits their records.
    sources: tuple[str, ...] = ()
    #: tree-sitter grammar ``collect`` gets those files parsed with; ``None`` = no tree (the
    #: fact reads the bytes, e.g. a ``<%@ ServiceHost %>`` directive).
    grammar: str | None = None


_DETECTORS: dict[str, list[Detector]] = {}
_FACTS: dict[str, list[FactSpec]] = {}
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


def register_fact(spec: FactSpec) -> FactSpec:
    if spec.language not in INDEX_HOOKED_LANGUAGES:
        raise RegistryError(
            f"fact {spec.name!r}: language {spec.language!r} has no index hook "
            f"(one of {sorted(INDEX_HOOKED_LANGUAGES)})"
        )
    if not spec.gate:
        raise RegistryError(f"fact {spec.name!r}: needs a gate")
    existing = _FACTS.setdefault(spec.language, [])
    for other in existing:
        if other.name == spec.name:
            return other  # idempotent re-import
    existing.append(spec)
    existing.sort(key=lambda f: f.name)
    return spec


def discover_detectors() -> None:
    """Import every module under ``parsers/`` that registers a detector or a fact (idempotent).

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
            text = py.read_bytes()
            if not any(m in text for m in _MARKERS):
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


def facts_for(language: str) -> list[FactSpec]:
    """Registered facts for ``language``, by name."""
    discover_detectors()
    return list(_FACTS.get(language, ()))


def fact_sources() -> dict[str, tuple[str, ...]]:
    """Language → the extra file extensions its facts collect from (see ``FactSpec.sources``)."""
    discover_detectors()
    return {
        lang: tuple(sorted({ext for spec in specs for ext in spec.sources}))
        for lang, specs in sorted(_FACTS.items())
        if any(spec.sources for spec in specs)
    }


def split_fact_sources(language: str, files: list[Path]) -> tuple[list[Path], list[Path]]:
    """``files`` handed to ``language``'s ``build_index`` → (its own files, fact-source files)."""
    exts = fact_sources().get(language, ())
    own = [f for f in files if not f.name.lower().endswith(exts)] if exts else list(files)
    foreign = [f for f in files if exts and f.name.lower().endswith(exts)]
    return own, foreign


def _collect_source_one(args: tuple[str, str, str, str]) -> dict[str, Any]:
    """Collect every fact of ``language`` that reads this fact-source file (picklable worker)."""
    language, file_s, rel, repo_root_s = args
    try:
        source = Path(file_s).read_bytes()
    except OSError:
        return {}
    out: dict[str, Any] = {}
    roots: dict[str, Node | None] = {}
    for spec in facts_for(language):
        if not rel.lower().endswith(spec.sources) or not any(g in source for g in spec.gate):
            continue
        try:
            if spec.grammar is not None and spec.grammar not in roots:
                from .treesitter import parse_source
                roots[spec.grammar] = parse_source(spec.grammar, source, 0).root_node
            value = spec.collect(roots.get(spec.grammar) if spec.grammar else None,
                                 source, rel, Path(repo_root_s))
        except Exception as exc:  # a fact never takes the index down: this file adds nothing
            from ..logging import get_logger
            get_logger("breezeai_cog.index").warning(
                "index.fact.skipped", path=rel, fact=spec.name, error=str(exc))
            continue
        if value is not None:
            out[spec.name] = value
    return out


def collect_fact_sources(
    language: str, files: list[Path], repo_root: Path, jobs: int = 1
) -> dict[str, list[Any]]:
    """Run ``language``'s facts over its fact-source files (from :func:`split_fact_sources`),
    across ``jobs`` processes. Returns fact name → collected values, in file order."""
    from ..utils import repo_relative
    from .index_common import parallel_map

    args = [(language, str(f), repo_relative(f, repo_root), str(repo_root)) for f in files]
    collected: dict[str, list[Any]] = {}
    for per_file in parallel_map(args, _collect_source_one, jobs):
        for name, value in per_file.items():
            collected.setdefault(name, []).append(value)
    return collected


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
        if not detector.gate.admits(ctx.source, is_fixture=is_fixture):
            continue
        found = detector.detect(dc)
        if found is None:
            continue
        apply(record, found.emits, ctx.source, ctx.path)
        if found.label and record.framework is None:
            record.framework = found.label


def collect_additive(
    language: str, root: Node, source: bytes, rel: str, repo_root: Path | None
) -> dict[str, Any]:
    """Run every fact ``collect`` registered for ``language`` on one file, from the language's
    ``build_index`` per-file pass. Returns fact name → collected value, for the facts whose
    gate matched and that found something."""
    out: dict[str, Any] = {}
    for spec in facts_for(language):
        if not any(g in source for g in spec.gate):
            continue
        value = spec.collect(root, source, rel, repo_root)
        if value is not None:
            out[spec.name] = value
    return out


def resolve_additive(language: str, collected: dict[str, list[Any]], index: Any) -> dict[str, Any]:
    """Resolve every fact for ``language`` once, after the language's index is reduced.
    ``collected`` maps fact name → its per-file values in file order. Returns fact name →
    fact (a fact that resolved to ``None`` is left out), to be stored on the index for
    :func:`index_fact`."""
    facts = {spec.name: spec.resolve(collected.get(spec.name, []), index)
             for spec in facts_for(language)}
    return {name: fact for name, fact in facts.items() if fact is not None}


def index_fact(index: Any | None, name: str) -> Any | None:
    """The fact named ``name``, or None (no index, e.g. single-file parsing, or a language whose
    index carries no facts)."""
    return (getattr(index, "facts", None) or {}).get(name)
