"""Analysis pipeline (ARCHITECTURE.md §5/§6).

``run`` drives the full analysis to a sink using the parallel executor and assembles
``projectMetaData``. ``iter_records`` streams ``(ScanEntry, FileRecord)`` sequentially
in-process for the library's ``iter_file_records``. The ``projectMetaData``-first
temp strategy lives in ``FileSink`` (§6).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .._version import __version__
from ..logging import get_logger
from ..schemas import FileRecord, ProjectMetaData
from . import executor
from .ignore import IgnoreEngine
from .registry import discover_builtin, parser_for, registered
from .scanner import ScanEntry, scan

log = get_logger("breezeai_cog.pipeline")


def _classifier(languages: set[str] | None) -> Callable[[str], str | None]:
    def classify(path: str) -> str | None:
        parser = parser_for(path)
        if parser is None:
            return None
        if languages and parser.name not in languages:
            return None
        return parser.name

    return classify


def _scan_entries(repo_root: Path, settings) -> Iterator[ScanEntry]:
    discover_builtin()
    engine = IgnoreEngine.build(registered())
    languages = set(settings.languages) if settings.languages else None

    def on_skip(path: str, reason: str) -> None:
        log.debug("scan.file.skipped", path=path, reason=reason)

    yield from scan(
        repo_root, _classifier(languages),
        engine=engine, max_file_size=settings.max_file_size, on_skip=on_skip,
    )


def _build_indexes(repo_root: Path, entries: list[ScanEntry]) -> dict:
    """Run each parser's optional ``build_index`` once (main process). Maps
    parser-name → index; threaded into ParseContext.resolution_index."""
    parsers: dict[str, object] = {}
    files: dict[str, list[Path]] = {}
    for entry in entries:
        parser = parser_for(entry.path)
        if parser is None:
            continue
        parsers[parser.name] = parser
        files.setdefault(parser.name, []).append(repo_root / entry.path)
    indexes: dict[str, object] = {}
    for name, parser in parsers.items():
        build = getattr(parser, "build_index", None)
        if build is None:
            continue
        index = build(repo_root, files[name])
        if index is not None:
            indexes[name] = index
    return indexes


def iter_records(repo_root: str | Path, settings) -> Iterator[tuple[ScanEntry, FileRecord]]:
    """Sequential, in-process scan + parse (streaming)."""
    repo_root = Path(repo_root)
    entries = list(_scan_entries(repo_root, settings))
    options = executor._options(settings)
    options["indexes"] = _build_indexes(repo_root, entries)
    for entry in entries:
        record = executor._parse_entry(entry.path, str(repo_root), options)
        if record is not None:
            yield entry, record


def run(repo_root: str | Path, settings, sink) -> ProjectMetaData:
    """Full analysis to a sink (parallel) → assembled projectMetaData."""
    repo_root = Path(repo_root)
    entries = list(_scan_entries(repo_root, settings))
    indexes = _build_indexes(repo_root, entries)

    total_files = total_functions = total_classes = total_loc = config_files = 0
    languages: set[str] = set()
    by_type: dict[str, int] = {}

    for language, record in executor.parse_entries(entries, repo_root, settings, indexes):
        sink.write(record)
        total_files += 1
        total_functions += len(record.functions)
        total_classes += len(record.classes)
        total_loc += record.loc
        languages.add(language)
        by_type[language] = by_type.get(language, 0) + 1
        if record.type == "config":
            config_files += 1

    meta = ProjectMetaData(
        repositoryPath=str(repo_root.resolve()),
        repositoryName=repo_root.resolve().name,
        analyzedLanguages=sorted(languages),
        totalFiles=total_files,
        totalFunctions=total_functions,
        totalClasses=total_classes,
        totalLinesOfCode=total_loc,
        configs={"totalConfigFiles": config_files, "byType": by_type, "packageManagers": []},
        generatedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        toolVersion=__version__,
    )
    sink.finalize(meta)
    log.info(
        "analysis.complete",
        files=total_files, functions=total_functions, classes=total_classes,
        loc=total_loc, languages=sorted(languages),
    )
    return meta
