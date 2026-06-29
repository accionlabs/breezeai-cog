"""Streaming gzip helpers — never hold the full dataset in memory."""

from __future__ import annotations

import gzip
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

DEFAULT_LEVEL = 6


@contextmanager
def open_gzip_text(path: str | Path, level: int = DEFAULT_LEVEL) -> Iterator[IO[str]]:
    """Open a gzip file for streaming UTF-8 text writes."""
    fh = gzip.open(path, "wt", encoding="utf-8", compresslevel=level)
    try:
        yield fh
    finally:
        fh.close()
