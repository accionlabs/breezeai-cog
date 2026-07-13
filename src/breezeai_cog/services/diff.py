"""Diff streaming for ``/api/analyze-diff``: parse the acquired temp dir,
stream FileRecord NDJSON.gz to S3 (filtered to changed files in incremental mode),
and accumulate ``projectMetaData`` **out-of-band** (it rides the notification, not the
gz stream). Mirrors ``runAnalysisDiffStream`` in ``server.js``."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._version import __version__
from ..config import Settings
from ..core import pipeline
from ..emit.ndjson import to_line
from ..schemas import FileRecord


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _S3StreamSink:
    """Streams (optionally filtered) FileRecords to an open S3 upload and tallies
    projectMetaData over the records actually written. The meta is delivered
    out-of-band, so ``finalize`` writes nothing to the stream."""

    def __init__(self, upload: Any, filter_paths: set[str] | None) -> None:
        self._upload = upload
        self._filter = filter_paths
        self.files = self.funcs = self.classes = self.loc = self.config = 0
        self.languages: set[str] = set()
        self.by_type: dict[str, int] = {}

    def write(self, record: FileRecord) -> None:
        if self._filter is not None and record.path not in self._filter:
            return
        self._upload.write_line(to_line(record))
        self.files += 1
        self.funcs += len(record.functions)
        self.classes += len(record.classes)
        self.loc += record.loc
        self.languages.add(record.language)
        self.by_type[record.language] = self.by_type.get(record.language, 0) + 1
        if record.type == "config":
            self.config += 1

    def finalize(self, _meta: Any) -> None:  # out-of-band; nothing to the stream
        pass


def run_diff_stream(
    settings: Settings, upload: Any, temp_dir: str | Path, filter_set: set[str] | None, repo_name: str
) -> dict[str, Any]:
    sink = _S3StreamSink(upload, filter_set)
    pipeline.run_inprocess(temp_dir, settings, sink)
    upload.close()
    return {
        "repositoryPath": repo_name,
        "repositoryName": repo_name,
        "analyzedLanguages": sorted(sink.languages),
        "totalFiles": sink.files,
        "totalFunctions": sink.funcs,
        "totalClasses": sink.classes,
        "totalLinesOfCode": sink.loc,
        "configs": {"totalConfigFiles": sink.config, "byType": sink.by_type, "packageManagers": []},
        "generatedAt": _now(),
        "toolVersion": __version__,
    }


def empty_meta(repo_name: str) -> dict[str, Any]:
    """Fully-shaped projectMetaData for a deletion-only commit (no files parsed)."""
    return {
        "repositoryPath": repo_name,
        "repositoryName": repo_name,
        "analyzedLanguages": [],
        "totalFiles": 0,
        "totalFunctions": 0,
        "totalClasses": 0,
        "totalLinesOfCode": 0,
        "configs": {},
        "generatedAt": _now(),
        "toolVersion": __version__,
    }
