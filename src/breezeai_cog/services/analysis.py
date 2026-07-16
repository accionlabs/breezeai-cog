"""AnalysisService — config → acquire → pipeline → sink → summary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from ..config import Settings
from ..core import pipeline
from ..emit.sinks import FileSink, Sink
from ..schemas import FileRecord, ProjectMetaData


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    project_meta: ProjectMetaData
    out_path: Path | None
    written: bool = True  # False when the parser produced nothing and no file was emitted


class AnalysisService:
    """Drives a repo analysis. Settings are injected (no global)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _default_out(self, repo: Path) -> Path:
        """Resolve the output file inside the output **directory** (`--out`, default the
        repo's parent): `<out_dir>/<repo-name>-project-analysis.ndjson.gz`."""
        repo = repo.resolve()
        out_dir = Path(self.settings.out) if self.settings.out is not None else repo.parent
        return out_dir / f"{repo.name}-project-analysis.ndjson.gz"

    def analyze_repo(
        self,
        repo: str | Path,
        *,
        sink: Sink | None = None,
        progress: Callable[[int, int], None] | None = None,
        summary_out: dict | None = None,
        log_summary: bool = True,
    ) -> AnalysisResult:
        repo = Path(repo)
        owns_sink = sink is None
        out_path = self._default_out(repo) if owns_sink else None
        sink = sink or FileSink(out_path)  # type: ignore[arg-type]
        meta = pipeline.run(
            repo, self.settings, sink,
            progress=progress, summary_out=summary_out, log_summary=log_summary,
        )
        # A FileSink skips emitting a file when nothing was parsed; caller-supplied sinks
        # own their own semantics, so assume they always "wrote".
        written = sink.wrote if isinstance(sink, FileSink) else True
        return AnalysisResult(project_meta=meta, out_path=out_path, written=written)

    def iter_file_records(self, repo: str | Path) -> Iterator[FileRecord]:
        for _entry, record in pipeline.iter_records(repo, self.settings):
            yield record
