"""Analysis pipeline (ARCHITECTURE.md §5/§6).

``run`` drives the full analysis to a sink using the parallel executor and assembles
``projectMetaData``. ``iter_records`` streams ``(ScanEntry, FileRecord)`` sequentially
in-process for the library's ``iter_file_records``. The ``projectMetaData``-first
temp strategy lives in ``FileSink`` (§6).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .._version import __version__
from ..logging import get_logger
from ..schemas import FileRecord, ProjectMetaData
from . import executor
from .ignore import IgnoreEngine
from .registry import base_parser_for, discover_builtin, registered
from .scanner import ScanEntry, scan

log = get_logger("breezeai_cog.pipeline")


def _classifier(languages: set[str] | None) -> Callable[[str], str | None]:
    def classify(path: str) -> str | None:
        base = base_parser_for(path)  # the language parser (extension allow-list + label)
        if base is None:
            return None
        if languages and base.name not in languages:
            return None
        return base.name

    return classify


def _scan_entries(
    repo_root: Path, settings, skips: dict[str, int] | None = None
) -> Iterator[ScanEntry]:
    discover_builtin()
    engine = IgnoreEngine.build(registered())
    languages = set(settings.languages) if settings.languages else None
    debug_on = settings.log_level == "DEBUG"

    def on_skip(path: str, reason: str) -> None:
        if skips is not None:  # cheap tally for the run summary (ignored/unsupported/oversized)
            skips[reason] = skips.get(reason, 0) + 1
        if debug_on:  # gated so structlog never renders below DEBUG
            log.debug("scan.file.skipped", path=path, reason=reason)

    for entry in scan(
        repo_root, _classifier(languages),
        engine=engine, max_file_size=settings.max_file_size, on_skip=on_skip,
    ):
        # Per-language layer-2 filter (§9), applied post-scan and scoped to the file's
        # own classified language — so e.g. C#'s NuGet ``packages/`` never prunes a
        # TypeScript ``packages/`` workspace. Universal built-ins already pruned the walk.
        if engine.is_lang_ignored(entry.path, entry.language) and not (
            engine.is_lang_included(entry.path, entry.language)
            or engine.is_included_global(entry.path)
        ):
            on_skip(entry.path, "ignored")
            continue
        yield entry


def _build_indexes(
    repo_root: Path, entries: list[ScanEntry], *, debug_on: bool = False
) -> dict:
    """Run each parser's optional ``build_index`` once (main process). Maps
    parser-name → index; threaded into ParseContext.resolution_index."""
    bases: dict[str, object] = {}
    files: dict[str, list[Path]] = {}
    for entry in entries:
        base = base_parser_for(entry.path)  # index is per base language, keyed by its name
        if base is None:
            continue
        bases[base.name] = base
        files.setdefault(base.name, []).append(repo_root / entry.path)
    indexes: dict[str, object] = {}
    for name, base in bases.items():
        start = time.perf_counter()
        index = base.build_index(repo_root, files[name])
        if debug_on:
            log.debug(
                "build_index.done", parser=name, files=len(files[name]),
                ms=round((time.perf_counter() - start) * 1000, 1), built=index is not None,
            )
        if index is not None:
            indexes[name] = index
    return indexes


def iter_records(repo_root: str | Path, settings) -> Iterator[tuple[ScanEntry, FileRecord]]:
    """Sequential, in-process scan + parse (streaming)."""
    repo_root = Path(repo_root)
    entries = list(_scan_entries(repo_root, settings))
    options = executor._options(settings)
    options["indexes"] = _build_indexes(
        repo_root, entries, debug_on=settings.log_level == "DEBUG"
    )
    for entry in entries:
        record = executor._parse_entry(entry.path, str(repo_root), options)
        if record is not None:
            yield entry, record


class _ConfigSummary:
    """Aggregates config-file ``metadata`` into ``projectMetaData.configs`` — category
    counts, package managers / build tools, docker info, and dependency totals."""

    def __init__(self) -> None:
        self.by_type: dict[str, int] = {}
        self.package_managers: set[str] = set()
        self.build_tools: set[str] = set()
        self.services: set[str] = set()
        self.ports: set[str] = set()
        self.has_dockerfile = self.has_compose = False
        self.dep_total = self.dep_prod = self.dep_dev = 0

    def add(self, md: dict) -> None:
        self.by_type[md.get("category", "other")] = self.by_type.get(md.get("category", "other"), 0) + 1
        if md.get("packageManager"):
            self.package_managers.add(md["packageManager"])
        if md.get("buildTool"):
            self.build_tools.add(md["buildTool"])
        self.dep_total += md.get("dependencyCount", 0)
        if md.get("kind") == "package.json":
            self.dep_prod += md.get("dependencyCount", 0)
            self.dep_dev += md.get("devDependencyCount", 0)
        if md.get("kind") == "dockerfile":
            self.has_dockerfile = True
            self.ports.update((md.get("dockerInfo") or {}).get("exposedPorts", []))
        if (dc := md.get("dockerCompose")):
            self.has_compose = True
            self.services.update(dc.get("services", []))
            self.ports.update(dc.get("exposedPorts", []))

    def result(self, total: int) -> dict:
        return {
            "totalConfigFiles": total,
            "byType": self.by_type,
            "packageManagers": sorted(self.package_managers),
            "buildTools": sorted(self.build_tools),
            "docker": {
                "hasDockerfile": self.has_dockerfile, "hasCompose": self.has_compose,
                "services": sorted(self.services), "exposedPorts": sorted(self.ports),
            },
            "dependencies": {"total": self.dep_total, "production": self.dep_prod,
                             "development": self.dep_dev},
        }


def _assemble(
    repo_root: Path,
    records: Iterator[tuple[str, FileRecord]],
    sink,
    *,
    candidates: int | None = None,
    skips: dict[str, int] | None = None,
    debug_on: bool = False,
    progress: Callable[[int, int], None] | None = None,
    summary_out: dict | None = None,
    log_summary: bool = True,
) -> ProjectMetaData:
    """Stream (language, record) pairs to the sink and accumulate projectMetaData.

    Logs an ``analysis.complete`` summary (candidate files, parsed, failed, skipped +
    cumulative totals) at INFO, or DEBUG when ``log_summary`` is False (the caller will
    present it itself, e.g. as a table). If ``summary_out`` is given it's filled with the
    same numbers. ``progress(done, total)`` — if given — is called as records arrive.
    Under ``--verbose`` a per-file ``parse.file.done`` is also logged.
    """
    total_files = total_functions = total_classes = total_loc = config_files = 0
    total_statements = 0
    languages: set[str] = set()
    cfg = _ConfigSummary()

    if progress is not None and candidates is not None:
        progress(0, candidates)  # establish the total up front

    for language, record in records:
        sink.write(record)
        total_files += 1
        total_functions += len(record.functions)
        total_classes += len(record.classes)
        total_statements += len(record.statements)
        total_loc += record.loc
        if record.type == "config":
            config_files += 1
            cfg.add(record.metadata or {})
        else:
            languages.add(language)  # config is not a programming language
        if progress is not None and candidates is not None:
            progress(total_files, candidates)
        if debug_on:  # gated: no structlog cost unless --verbose
            log.debug(
                "parse.file.done", path=record.path, language=language,
                functions=len(record.functions), classes=len(record.classes),
                statements=len(record.statements),
            )

    meta = ProjectMetaData(
        repositoryPath=str(repo_root.resolve()),
        repositoryName=repo_root.resolve().name,
        analyzedLanguages=sorted(languages),
        totalFiles=total_files,
        totalFunctions=total_functions,
        totalClasses=total_classes,
        totalLinesOfCode=total_loc,
        configs=cfg.result(config_files),
        generatedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        toolVersion=__version__,
    )
    sink.finalize(meta)

    skips = skips or {}
    parsed = total_files
    candidate_n = candidates if candidates is not None else parsed  # source files to parse
    failed = max(0, candidate_n - parsed)                            # candidates that errored
    skipped_total = sum(skips.values())                             # non-candidates dropped in scan
    scanned = candidate_n + skipped_total                           # = parsed + failed + skipped
    if summary_out is not None:
        summary_out.update(
            scanned=scanned, parsed=parsed, failed=failed,
            skipped=skipped_total, skips=dict(skips), statements=total_statements,
        )
    emit = log.info if log_summary else log.debug
    emit(
        "analysis.complete",
        scanned=scanned,                   # files walked = parsed + failed + skipped
        parsed=parsed,                     # records produced
        failed=failed,                     # candidates that errored out
        skipped=skipped_total or None,     # ignored + unsupported + oversized
        skips=skips or None,
        functions=total_functions, classes=total_classes,
        statements=total_statements, loc=total_loc,
        languages=sorted(languages),
    )
    return meta


def run(
    repo_root: str | Path, settings, sink,
    *,
    progress: Callable[[int, int], None] | None = None,
    summary_out: dict | None = None,
    log_summary: bool = True,
) -> ProjectMetaData:
    """Full analysis to a sink (parallel) → assembled projectMetaData.

    ``progress(done, total)`` — if given — receives live counts as files complete.
    ``summary_out`` / ``log_summary`` — see :func:`_assemble`.
    """
    repo_root = Path(repo_root)
    debug_on = settings.log_level == "DEBUG"
    skips: dict[str, int] = {}
    entries = list(_scan_entries(repo_root, settings, skips))
    indexes = _build_indexes(repo_root, entries, debug_on=debug_on)
    records = executor.parse_entries(entries, repo_root, settings, indexes)
    return _assemble(
        repo_root, records, sink, candidates=len(entries), skips=skips,
        debug_on=debug_on, progress=progress,
        summary_out=summary_out, log_summary=log_summary,
    )


def run_inprocess(repo_root: str | Path, settings, sink) -> ProjectMetaData:
    """Full analysis to a sink, **sequential and in-process** (no spawn pool) — the
    server `/api/analyze` path (§10), where file lists are small and per-request pool
    startup would dominate."""
    repo_root = Path(repo_root)
    debug_on = settings.log_level == "DEBUG"
    skips: dict[str, int] = {}
    entries = list(_scan_entries(repo_root, settings, skips))
    options = executor._options(settings)
    options["indexes"] = _build_indexes(repo_root, entries, debug_on=debug_on)

    def gen() -> Iterator[tuple[str, FileRecord]]:
        for entry in entries:
            record = executor._parse_entry(entry.path, str(repo_root), options)
            if record is not None:
                yield record.language, record

    return _assemble(
        repo_root, gen(), sink, candidates=len(entries), skips=skips, debug_on=debug_on
    )
