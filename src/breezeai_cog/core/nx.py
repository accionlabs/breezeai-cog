"""Nx workspace discovery and deterministic file-to-project assignment.

Nx project ownership is a repository fact that applies to files of every language, so it is
not a parser concern. This module builds a small, picklable index in the parent process; the
executor stamps ``project`` / ``projectRoot`` on each record after it is parsed.

* A project is a ``project.json`` inside an Nx workspace (a ``nx.json`` at or above it), or an
  entry with a ``root`` in the project map of the repository's ``nx.json`` / ``workspace.json``
  (older Nx). Any other ``project.json`` belongs to another tool and is not read.
* A file belongs to the project whose root is its nearest ancestor (``apps/webx`` is not inside
  ``apps/web``).
* The name is the one the configuration writes: ``"name"`` of ``project.json``, else the key of
  the project map for that root. Without one, ``project`` is left empty (Nx itself rejects a
  project without a name); ``projectRoot`` still groups the files. A root is never inferred
  from ``sourceRoot``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from ..parsers.config.nx import in_nx_workspace
from .scanner import ScanEntry


@dataclass(frozen=True, slots=True)
class NxProject:
    """One Nx project root and the name its configuration gives it (None when it gives none)."""

    name: str | None
    root: str  # repository-relative POSIX path; ``""`` is the repository root


@dataclass(frozen=True, slots=True)
class NxProjectIndex:
    """Immutable project roots, sorted from most-specific to least-specific."""

    projects: tuple[NxProject, ...] = ()

    def project_for(self, path: str) -> NxProject | None:
        """Return the nearest project containing ``path``, or ``None`` outside Nx roots."""
        rel = _normalise_rel(path)
        for project in self.projects:
            if not project.root or rel == project.root or rel.startswith(project.root + "/"):
                return project
        return None


def discover_nx_projects(repo_root: Path, entries: Sequence[ScanEntry]) -> NxProjectIndex:
    """The Nx projects of the repository: ``project.json`` files of an Nx workspace, and the
    project maps of a root ``nx.json`` / ``workspace.json``.

    ``entries`` is used for project files so normal scanner scope (ignore rules and language
    filters) is respected. The root ``nx.json`` / ``workspace.json`` are read directly: older
    layouts keep project ownership there, and a language filter may leave them out.
    """
    explicit: dict[str, str | None] = {}
    for entry in entries:
        rel = _normalise_rel(entry.path)
        if PurePosixPath(rel).name != "project.json" or not in_nx_workspace(rel, repo_root):
            continue
        name = _read_json(repo_root / rel).get("name")
        root = _normalise_rel(str(PurePosixPath(rel).parent))
        explicit[root] = (name.strip() or None) if isinstance(name, str) else None

    legacy: dict[str, str] = {}
    if (repo_root / "nx.json").is_file():
        for filename in ("nx.json", "workspace.json"):
            path = repo_root / filename
            if not path.is_file():
                continue
            for key, value in _project_map(_read_json(path)).items():
                map_root = _map_root(value)
                if map_root is not None and isinstance(key, str) and key.strip():
                    legacy.setdefault(map_root, key.strip())

    # A per-project project.json is authoritative over an older root-level map; a project.json
    # without a name keeps the map's name for the same root.
    merged = {root: NxProject(name=name, root=root) for root, name in legacy.items()}
    for root, name in explicit.items():
        merged[root] = NxProject(name=name or legacy.get(root), root=root)
    return NxProjectIndex(tuple(sorted(merged.values(), key=_project_sort_key)))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(_strip_json_comments(text))
    except (OSError, ValueError, TypeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _project_map(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("projects")
    return value if isinstance(value, dict) else {}


def _map_root(value: Any) -> str | None:
    """The root a project-map entry declares: ``"apps/web"`` or ``{"root": "apps/web"}``.

    ``sourceRoot`` alone does not say where the project is (``apps/web`` or ``apps/web/src``),
    so an entry without a root is skipped."""
    raw = value.get("root") if isinstance(value, dict) else value
    if not isinstance(raw, str):
        return None
    root = _normalise_rel(raw)
    if root == ".." or root.startswith("../") or root.startswith("/"):
        return None
    if PurePosixPath(root).name == "project.json":
        root = _normalise_rel(str(PurePosixPath(root).parent))
    return root


def _project_sort_key(project: NxProject) -> tuple[int, str]:
    return (-len(project.root.split("/")) if project.root else 0, project.root)


def _normalise_rel(path: str) -> str:
    value = path.replace("\\", "/")
    value = os.path.normpath(value).replace("\\", "/")
    if value in (".", ""):
        return ""
    while value.startswith("./"):
        value = value[2:]
    return value


def _strip_json_comments(text: str) -> str:
    """Accept Nx's common JSONC form without treating URLs or strings as comments."""
    out: list[str] = []
    i, n = 0, len(text)
    in_string = escaped = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and text[i:i + 2] != "*/":
                i += 1
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)
