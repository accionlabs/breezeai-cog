"""Nx project index: nearest-root precedence, legacy maps, outside-project files."""

from __future__ import annotations

import json
import pickle

from breezeai_cog.core.nx import NxProject, NxProjectIndex, discover_nx_projects
from breezeai_cog.core.scanner import ScanEntry


def _entries(*paths: str) -> list[ScanEntry]:
    return [ScanEntry(path=p, language="config") for p in paths]


def test_nearest_ancestor_wins_and_outside_is_unset() -> None:
    index = NxProjectIndex((
        NxProject("nested", "apps/web/src/nested"),
        NxProject("web", "apps/web"),
    ))
    assert index.project_for("apps/web/src/nested/a.ts").name == "nested"
    assert index.project_for("apps/web/src/b.ts").name == "web"
    assert index.project_for("apps/webx/c.ts") is None  # prefix is not ancestry
    assert index.project_for("tools/d.ts") is None


def test_discovery_name_fallback_legacy_and_pickle(tmp_path) -> None:
    (tmp_path / "libs" / "shared").mkdir(parents=True)
    (tmp_path / "libs" / "shared" / "project.json").write_text("{}")
    (tmp_path / "workspace.json").write_text(json.dumps({
        "projects": {"old": "apps/old", "web": {"root": "libs/shared"}, "bad": 3},
    }))
    index = discover_nx_projects(tmp_path, _entries("libs/shared/project.json"))
    by_root = {p.root: p.name for p in index.projects}
    # project.json is authoritative over the legacy map; name falls back to the directory
    assert by_root == {"libs/shared": "shared", "apps/old": "old"}
    assert pickle.loads(pickle.dumps(index)) == index
