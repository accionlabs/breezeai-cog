"""breezeai-cog — Python code-ontology generator (capture side).

Parses source repositories into the capture NDJSON contract.

Public, semver-guaranteed surface (everything else is internal):
``analyze_repo``, ``iter_file_records``, ``capabilities``, and the schema models.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ._version import __version__
from .config import Settings
from .schemas import (
    Call,
    Class,
    Decorator,
    FileRecord,
    Function,
    Parameter,
    ProjectMetaData,
    Statement,
)
from .services import AnalysisResult, AnalysisService

__all__ = [
    "__version__",
    "analyze_repo",
    "iter_file_records",
    "capabilities",
    "AnalysisResult",
    "FileRecord",
    "Class",
    "Function",
    "Statement",
    "Parameter",
    "Call",
    "Decorator",
    "ProjectMetaData",
]


def analyze_repo(
    path: str | Path,
    *,
    languages: list[str] | None = None,
    capture_statements: bool = False,
    out: str | Path | None = None,
    jobs: int | None = None,
) -> AnalysisResult:
    """Analyze a local repository to a gzipped NDJSON file. High-level API."""
    settings = Settings(
        repo=Path(path),
        languages=languages,
        capture_statements=capture_statements,
        out=Path(out) if out is not None else None,
        jobs=jobs,
    )
    return AnalysisService(settings).analyze_repo(path)


def iter_file_records(
    path: str | Path,
    *,
    languages: list[str] | None = None,
    capture_statements: bool = False,
) -> Iterator[FileRecord]:
    """Stream ``FileRecord``s for programmatic consumption (no file written)."""
    settings = Settings(
        repo=Path(path), languages=languages, capture_statements=capture_statements
    )
    return AnalysisService(settings).iter_file_records(path)


def capabilities() -> dict:
    """Languages / frameworks / statement types / schema version the tool supports."""
    from .core.registry import capabilities as _caps
    from .core.registry import discover_builtin

    discover_builtin()
    return _caps()
