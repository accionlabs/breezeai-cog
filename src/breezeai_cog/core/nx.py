"""Nx workspace discovery and deterministic file-to-project assignment.

Nx project ownership is a repository fact, not a parser concern.  This module builds the
small, picklable index in the parent process and the executor uses it after each file is
parsed.  Resolution is deliberately lexical: the nearest discovered project root wins;
unknown roots and project names are left unset rather than inferred from arbitrary folders.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .scanner import ScanEntry


@dataclass(frozen=True, slots=True)
class NxProject:
    """One verified Nx project root and its graph-facing name."""

    name: str
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
    """Discover explicit ``project.json`` roots and legacy project maps.

    ``entries`` is used for project files so normal scanner scope (ignore rules and language
    filters) is respected.  Root ``nx.json`` / ``workspace.json`` are checked directly because
    they are the source of ownership for older layouts and may be outside a language filter.
    """
    explicit: dict[str, NxProject] = {}
    project_paths = {
        _normalise_rel(entry.path): repo_root / entry.path
        for entry in entries
        if Path(entry.path).name == "project.json"
    }
    for rel, path in sorted(project_paths.items()):
        data = _read_json(path)
        root = _normalise_rel(str(PurePosixPath(rel).parent))
        name = _project_name(data, root, repo_root)
        if name:
            explicit[root] = NxProject(name=name, root=root)

    legacy: dict[str, NxProject] = {}
    for filename in ("nx.json", "workspace.json"):
        path = repo_root / filename
        if not path.is_file():
            continue
        data = _read_json(path)
        for name, value in _project_map(data).items():
            map_root = _map_root(value)
            if map_root is None:
                continue
            project_name = name.strip()
            if project_name:
                legacy.setdefault(map_root, NxProject(name=project_name, root=map_root))

    # A real per-project project.json is authoritative over an older root-level map.
    merged = {**legacy, **explicit}
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


def _project_name(data: dict[str, Any], root: str, repo_root: Path) -> str:
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    # The fallback is the directory explicitly containing project.json.  For a root project,
    # the repository directory is the only available directory identity.
    return PurePosixPath(root).name if root else repo_root.name


def _map_root(value: Any) -> str | None:
    raw: Any = value
    if isinstance(value, dict):
        raw = value.get("root") if "root" in value else None
        if raw is None:
            source_root = value.get("sourceRoot")
            raw = str(PurePosixPath(source_root).parent) if isinstance(source_root, str) else None
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
