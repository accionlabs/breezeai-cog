"""Path helpers — repo-relative normalization (always POSIX in output)."""

from __future__ import annotations

import os
from pathlib import Path


def repo_relative(path: str | Path, repo_root: str | Path) -> str:
    """``path`` relative to ``repo_root`` as a POSIX string (stable across OSes)."""
    return Path(os.path.relpath(path, repo_root)).as_posix()
