"""Output sinks: the gzipped-NDJSON file sink and the in-memory sink. (The streaming
S3 upload lives in ``emit/s3.py``.)

The file sink implements the ``projectMetaData``-first strategy: body ``FileRecord`` lines stream to a temp NDJSON while totals accumulate;
``finalize`` writes the ``projectMetaData`` line first, then streams the body in,
compressed with streaming gzip. Memory stays bounded.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from ..schemas import FileRecord, ProjectMetaData
from .gzip_stream import DEFAULT_LEVEL, open_gzip_text
from .ndjson import to_line


class Sink(Protocol):
    """A destination for capture output."""

    def write(self, record: FileRecord) -> None: ...

    def finalize(self, project_meta: ProjectMetaData) -> None: ...


class MemorySink:
    """Collects records + meta in memory. Used by the server `/api/analyze` path,
    which returns a plain JSON `{ projectMetaData, files }` (no gzip)."""

    def __init__(self) -> None:
        self.records: list[FileRecord] = []
        self.project_meta: ProjectMetaData | None = None

    def write(self, record: FileRecord) -> None:
        self.records.append(record)

    def finalize(self, project_meta: ProjectMetaData) -> None:
        self.project_meta = project_meta


class FileSink:
    """Writes ``<out>.ndjson.gz`` with ``projectMetaData`` as the first line.

    If the analysis captured no real content (see :meth:`ProjectMetaData.has_content` —
    e.g. a folder whose only file is a trivial config), ``finalize`` emits no file at all
    and leaves :attr:`wrote` ``False`` — an empty ontology is never useful downstream
    (and would upload as a no-op)."""

    def __init__(self, out_path: str | Path, *, gzip_level: int = DEFAULT_LEVEL) -> None:
        self.out_path = Path(out_path)
        self._gzip_level = gzip_level
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        # A per-run randomized temp name (not a deterministic ``<name>.body.tmp``) so two
        # concurrent runs against the same repo — e.g. parallel CI jobs — get isolated temp
        # files and don't collide (one deleting the other's temp mid-run).
        self._body = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.out_path.parent,
            prefix=self.out_path.name + ".",
            suffix=".body.tmp",
            delete=False,
        )
        self._tmp = Path(self._body.name)
        self._finalized = False
        self.wrote = False  # True once finalize writes an actual file

    def write(self, record: FileRecord) -> None:
        self._body.write(to_line(record))

    def finalize(self, project_meta: ProjectMetaData) -> None:
        if self._finalized:
            raise RuntimeError("FileSink already finalized")
        self._body.close()
        self._finalized = True
        if not project_meta.has_content():  # nothing worth persisting — write no file
            self._tmp.unlink(missing_ok=True)
            return
        with open_gzip_text(self.out_path, self._gzip_level) as out:
            out.write(to_line(project_meta))  # projectMetaData first
            with self._tmp.open("r", encoding="utf-8") as body:
                shutil.copyfileobj(body, out)
        self._tmp.unlink(missing_ok=True)
        self.wrote = True
