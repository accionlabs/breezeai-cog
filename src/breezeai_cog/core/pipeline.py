"""Analysis pipeline (ARCHITECTURE.md §5/§6).

M2 runs single-process: scan → parse → assemble projectMetaData → sink. The
ProcessPoolExecutor fan-out and pool-initializer grammar loading arrive in M3; the
shape here (an ``iter_records`` generator + a ``run`` that drives a sink) is what
the parallel executor will slot into.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .._version import __version__
from ..logging import get_logger
from ..parsers.base import ParseContext
from ..schemas import FileRecord, ProjectMetaData
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


def iter_records(repo_root: str | Path, settings) -> Iterator[tuple[ScanEntry, FileRecord]]:
    """Scan + parse, yielding ``(ScanEntry, FileRecord)``. A file that fails to parse
    is logged and skipped (the repo always terminates)."""
    discover_builtin()
    repo_root = Path(repo_root)
    engine = IgnoreEngine.build(registered())
    languages = set(settings.languages) if settings.languages else None

    def on_skip(path: str, reason: str) -> None:
        log.debug("scan.file.skipped", path=path, reason=reason)

    for entry in scan(
        repo_root, _classifier(languages),
        engine=engine, max_file_size=settings.max_file_size, on_skip=on_skip,
    ):
        parser = parser_for(entry.path)
        if parser is None:  # pragma: no cover - classify already filtered
            continue
        abs_path = repo_root / entry.path
        try:
            ctx = ParseContext(
                path=entry.path,
                abs_path=abs_path,
                source=abs_path.read_bytes(),
                repo_root=repo_root,
                capture_statements=settings.capture_statements,
                text_truncation_limit=settings.text_truncation_limit,
            )
            record = parser.parse_file(ctx)
        except Exception as exc:  # per-file isolation (§5)
            log.warning("parse.file.failed", path=entry.path, parser=parser.name, error=str(exc))
            continue
        yield entry, record


def run(repo_root: str | Path, settings, sink) -> ProjectMetaData:
    """Drive the full analysis to a sink and return the assembled projectMetaData."""
    repo_root = Path(repo_root)
    total_files = total_functions = total_classes = total_loc = config_files = 0
    languages: set[str] = set()
    by_type: dict[str, int] = {}

    for entry, record in iter_records(repo_root, settings):
        sink.write(record)
        total_files += 1
        total_functions += len(record.functions)
        total_classes += len(record.classes)
        total_loc += record.loc
        languages.add(entry.language)
        by_type[entry.language] = by_type.get(entry.language, 0) + 1
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
        files=total_files, functions=total_functions, classes=total_classes, loc=total_loc,
        languages=sorted(languages),
    )
    return meta
