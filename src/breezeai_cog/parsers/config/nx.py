"""Is a file inside an Nx workspace?

``project.json`` and ``workspace.json`` are names other tools use too (a .NET Core 1.0
``project.json``, an editor's ``workspace.json``). They are Nx files only when a directory
between them and the repository root holds ``nx.json``, which every Nx workspace has. The
JSON parser routes them to the Nx extractors only then, and the Nx project index
(``core.nx``) reads only those.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path, PurePosixPath


@lru_cache(maxsize=4096)
def _has_nx_json(directory: str) -> bool:
    return os.path.isfile(os.path.join(directory, "nx.json"))


def in_nx_workspace(rel_path: str, repo_root: Path) -> bool:
    """True when ``nx.json`` is in the file's directory or one above it, up to ``repo_root``."""
    root = str(repo_root)
    parent = PurePosixPath(rel_path.replace("\\", "/")).parent
    for directory in (parent, *parent.parents):
        rel = str(directory)
        if _has_nx_json(root if rel in (".", "") else os.path.join(root, rel)):
            return True
    return False
