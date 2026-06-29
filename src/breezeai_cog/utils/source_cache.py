"""Per-run, per-process file-content cache (the worker reads each file once)."""

from __future__ import annotations

from pathlib import Path


class SourceCache:
    """Caches file bytes by path for the lifetime of one worker process."""

    def __init__(self) -> None:
        self._bytes: dict[str, bytes] = {}

    def read_bytes(self, path: str | Path) -> bytes:
        key = str(path)
        cached = self._bytes.get(key)
        if cached is None:
            cached = Path(path).read_bytes()
            self._bytes[key] = cached
        return cached

    def read_text(self, path: str | Path, encoding: str = "utf-8", errors: str = "replace") -> str:
        return self.read_bytes(path).decode(encoding, errors)

    def clear(self) -> None:
        self._bytes.clear()
