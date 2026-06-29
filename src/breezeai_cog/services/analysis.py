"""AnalysisService — config → acquire → pipeline → sink → summary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ..config import Settings
from ..core import pipeline
from ..emit.sinks import FileSink, Sink
from ..schemas import FileRecord, ProjectMetaData


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    project_meta: ProjectMetaData
    out_path: Path | None


class AnalysisService:
    """Drives a repo analysis. Settings are injected (no global)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _default_out(self, repo: Path) -> Path:
        if self.settings.out is not None:
            return Path(self.settings.out)
        return repo.resolve().parent / f"{repo.resolve().name}-project-analysis.ndjson.gz"

    def analyze_repo(self, repo: str | Path, *, sink: Sink | None = None) -> AnalysisResult:
        repo = Path(repo)
        owns_sink = sink is None
        out_path = self._default_out(repo) if owns_sink else None
        sink = sink or FileSink(out_path)  # type: ignore[arg-type]
        meta = pipeline.run(repo, self.settings, sink)
        return AnalysisResult(project_meta=meta, out_path=out_path)

    def iter_file_records(self, repo: str | Path) -> Iterator[FileRecord]:
        for _entry, record in pipeline.iter_records(repo, self.settings):
            yield record
