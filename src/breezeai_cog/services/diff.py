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


class UploadError(RuntimeError):
    """A storage write failed while streaming. Raised instead of the provider's own
    exception so the route can tell an upload failure (``failedStep: "upload"``) from
    a parser failure (``parse_stream``) — both otherwise surface from the same call
    (BREEZEAI-520 / BREEZEAI-681)."""


class _InfraStreamSink:
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
        line = to_line(record)
        try:
            self._upload.write_line(line)
        except Exception as exc:  # S3 part upload / gzip pipe failures
            raise UploadError(str(exc)) from exc
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
    """Parse ``temp_dir`` and stream the (filtered) records into ``upload``. The caller
    owns ``upload`` and must ``close()`` it — closing is what finalises the multipart
    upload, and the route reports that step separately from parsing."""
    sink = _InfraStreamSink(upload, filter_set)
    pipeline.run_inprocess(temp_dir, settings, sink)
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
    """Fully-shaped projectMetaData for a commit with no files parsed (deletion-only, or
    no file changes at all)."""
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
