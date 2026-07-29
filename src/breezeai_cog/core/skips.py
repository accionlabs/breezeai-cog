"""Per-repo skip report.

Collects the files and directories the scanner dropped, grouped by reason, so the
CLI can print a summary and write a ``<repo>-skipped-report.json`` sidecar after each
repo. This is purely a reporting artifact — it does **not** feed the
``scanned = parsed + failed + skipped`` reconciliation.

Reasons (file-level, counted in :attr:`counts`):

- ``unsupported`` — no registered parser claims the file's extension.
- ``ignored`` — matched an ignore rule (built-in ``default_ignores.txt``,
  ``.gitignore`` / ``.repoignore``, or a per-language layer-2 rule) and was not
  re-included via ``.repoinclude``.
- ``oversized`` — larger than ``max_file_size`` (2 MB default).

Pruned **directories** are recorded in :attr:`dirs` but are intentionally **not**
counted in :attr:`counts` (they are not candidate files, so counting them would
break the file-level reconciliation).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SkipReport:
    #: reason -> count, file-level only (ignored / unsupported / oversized)
    counts: dict[str, int] = field(default_factory=dict)
    #: per-file skip records: {"path", "reason", optional "size"}
    files: list[dict[str, Any]] = field(default_factory=list)
    #: repo-relative paths of ignored (pruned) directories
    dirs: list[str] = field(default_factory=list)
    #: extension -> count, among ``unsupported`` files
    extensions: dict[str, int] = field(default_factory=dict)

    def record(self, path: str, reason: str, *, is_dir: bool = False, size: int | None = None) -> None:
        """Record one skipped file or directory."""
        if is_dir:
            self.dirs.append(path)  # not counted — dirs aren't candidate files
            return
        self.counts[reason] = self.counts.get(reason, 0) + 1
        entry: dict[str, Any] = {"path": path, "reason": reason}
        if size is not None:
            entry["size"] = size
        self.files.append(entry)
        if reason == "unsupported":
            ext = os.path.splitext(path)[1].lower() or "<none>"
            self.extensions[ext] = self.extensions.get(ext, 0) + 1

    @property
    def total_files(self) -> int:
        return sum(self.counts.values())

    @property
    def is_empty(self) -> bool:
        return not self.files and not self.dirs

    def top_extensions(self, n: int | None = None) -> list[tuple[str, int]]:
        """Extensions among unsupported files, most-common first."""
        ranked = sorted(self.extensions.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked[:n] if n is not None else ranked

    def to_dict(self) -> dict[str, Any]:
        """Serializable shape for the ``<repo>-skipped-report.json`` sidecar."""
        return {
            "summary": dict(self.counts),
            "totalSkippedFiles": self.total_files,
            "unsupportedExtensions": {ext: cnt for ext, cnt in self.top_extensions()},
            "ignoredDirectories": sorted(self.dirs),
            "files": self.files,
        }
