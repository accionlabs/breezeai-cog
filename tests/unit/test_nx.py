"""Nx workspaces: config routing, project discovery and file-to-project assignment."""

from __future__ import annotations

import gzip
import json
import pickle
from pathlib import Path
from typing import Any

import pytest

from breezeai_cog import analyze_repo, iter_file_records
from breezeai_cog.core.nx import NxProject, NxProjectIndex, discover_nx_projects
from breezeai_cog.core.scanner import ScanEntry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.config.extractors import extract_config
from breezeai_cog.parsers.config.nx import in_nx_workspace
from breezeai_cog.parsers.structured_json.parser import JsonParser


def _write(root: Path, files: dict[str, Any]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content))
    return root


def _entries(*paths: str) -> list[ScanEntry]:
    return [ScanEntry(path=p, language="config") for p in paths]


def _json_record(repo: Path, rel: str) -> Any:
    ctx = ParseContext(path=rel, abs_path=repo / rel, source=(repo / rel).read_bytes(),
                       repo_root=repo)
    return JsonParser().parse_file(ctx)


# --------------------------------------------------------------------------- extractors

def test_project_json_keeps_what_the_file_writes() -> None:
    md = extract_config("apps/web/project.json", json.dumps({
        "name": "web", "tags": ["type:app", 3], "implicitDependencies": ["api"],
        "targets": {"build": {}, "test": {}},
    }))
    assert md["kind"] == "project.json" and md["buildTool"] == "nx"
    assert md["projectInfo"] == {"name": "web", "tags": ["type:app"],
                                 "implicitDependencies": ["api"], "targets": ["build", "test"]}


def test_project_json_without_name_has_no_name() -> None:
    md = extract_config("libs/shared/project.json", json.dumps({"targets": {"build": {}}}))
    assert md["projectInfo"]["name"] is None  # never the folder name
    assert "projectRoot" not in md["projectInfo"]


def test_nx_and_workspace_project_maps() -> None:
    nx = extract_config("nx.json", json.dumps({
        "plugins": [{"plugin": "@nx/js"}], "targetDefaults": {"build": {}},
        "projects": {"web": "apps/web"},
    }))
    assert nx["kind"] == "nx.json"
    assert nx["projectInfo"]["projects"] == {"web": {"root": "apps/web"}}
    assert nx["nxConfig"] == {"targetDefaults": ["build"], "plugins": ["@nx/js"]}

    ws = extract_config("workspace.json", json.dumps({"projects": {
        "api": {"root": "apps/api", "implicitDependencies": ["shared"], "targets": {"serve": {}}},
        "bad": 3,
    }}))
    assert ws["kind"] == "workspace.json" and ws["buildTool"] == "nx"
    assert ws["projectInfo"]["projects"] == {
        "api": {"root": "apps/api", "implicitDependencies": ["shared"], "targets": ["serve"]}
    }


# --------------------------------------------------------------------------- routing

def test_in_nx_workspace_looks_up_to_the_repo_root(tmp_path: Path) -> None:
    _write(tmp_path, {"nx.json": {}, "apps/web/project.json": {}, "other/sub/x.json": {}})
    assert in_nx_workspace("apps/web/project.json", tmp_path)
    assert in_nx_workspace("project.json", tmp_path)
    nested = _write(tmp_path / "n", {"tools/nx.json": {}, "tools/a/project.json": {}})
    assert in_nx_workspace("tools/a/project.json", nested)
    assert not in_nx_workspace("app/project.json", tmp_path / "n")


def test_project_json_is_rich_only_in_an_nx_workspace(tmp_path: Path) -> None:
    nx = _write(tmp_path / "nx", {"nx.json": {}, "apps/web/project.json": {"name": "web"},
                                  "workspace.json": {"projects": {}}})
    assert _json_record(nx, "apps/web/project.json").metadata["kind"] == "project.json"
    assert _json_record(nx, "workspace.json").metadata["kind"] == "workspace.json"

    dotnet = _write(tmp_path / "dotnet", {"legacy-api/project.json": {
        "dependencies": {"Microsoft.AspNetCore.Mvc": "1.0.0"}, "frameworks": {"netcoreapp1.0": {}},
    }})
    record = _json_record(dotnet, "legacy-api/project.json")
    assert record.language == "structured-json"  # captured in full, not `buildTool: nx`
    assert record.metadata["kind"] == "structured-json"


# --------------------------------------------------------------------------- discovery

def test_nearest_ancestor_wins_and_outside_is_unset() -> None:
    index = NxProjectIndex((NxProject("nested", "apps/web/src/nested"), NxProject("web", "apps/web")))
    assert index.project_for("apps/web/src/nested/a.ts") == NxProject("nested", "apps/web/src/nested")
    assert index.project_for("apps/web/src/b.ts") == NxProject("web", "apps/web")
    assert index.project_for("apps/webx/c.ts") is None  # a prefix is not an ancestor
    assert index.project_for("tools/d.ts") is None


def test_names_come_only_from_the_configuration(tmp_path: Path) -> None:
    repo = _write(tmp_path, {
        "nx.json": {},
        "project.json": {"targets": {}},  # root project, no name
        "libs/shared/project.json": {},  # no name, not in the map
        "apps/old/project.json": {},  # no name, but the map names its root
        "workspace.json": {"projects": {
            "old": "apps/old", "legacy": {"root": "apps/legacy"},
            "src-only": {"sourceRoot": "apps/web/src"},  # no root: skipped
        }},
    })
    index = discover_nx_projects(
        repo, _entries("project.json", "libs/shared/project.json", "apps/old/project.json")
    )
    assert {p.root: p.name for p in index.projects} == {
        "": None, "libs/shared": None, "apps/old": "old", "apps/legacy": "legacy",
    }
    assert index.project_for("apps/web/src/main.ts") == NxProject(None, "")  # not `apps`
    assert pickle.loads(pickle.dumps(index)) == index


def test_no_nx_json_means_no_projects(tmp_path: Path) -> None:
    repo = _write(tmp_path, {"legacy-api/project.json": {"name": "api"},
                             "workspace.json": {"projects": {"web": "apps/web"}}})
    assert discover_nx_projects(repo, _entries("legacy-api/project.json")).projects == ()


# --------------------------------------------------------------------------- pipeline

@pytest.mark.parametrize("jobs", [1, 2])
def test_two_apps_and_a_lib_through_the_pipeline(tmp_path: Path, jobs: int) -> None:
    repo = _write(tmp_path / "nx-repo", {
        "nx.json": {"targetDefaults": {"build": {}}},
        "apps/web/project.json": {"name": "web", "implicitDependencies": ["api"],
                                  "targets": {"build": {}}},
        "apps/web/src/main.ts": "export const web = true;\n",
        "apps/api/project.json": {"name": "api", "targets": {"build": {}}},
        "apps/api/src/main.ts": "export const api = true;\n",
        "libs/shared/project.json": {"targets": {"build": {}}},
        "libs/shared/src/index.ts": "export const shared = true;\n",
        "tools/outside.ts": "export const outside = true;\n",
    })
    result = analyze_repo(repo, jobs=jobs)
    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    records = {r["path"]: r for r in map(json.loads, lines[1:])}

    assert records["nx.json"]["metadata"]["kind"] == "nx.json"
    assert "project" not in records["nx.json"] and "projectRoot" not in records["nx.json"]
    web = records["apps/web/project.json"]
    assert web["language"] == "config" and web["metadata"]["kind"] == "project.json"
    assert web["metadata"]["projectInfo"]["implicitDependencies"] == ["api"]  # web -> api
    assert records["apps/web/src/main.ts"]["project"] == "web"
    assert records["apps/web/src/main.ts"]["projectRoot"] == "apps/web"
    assert records["apps/api/src/main.ts"]["project"] == "api"
    lib = records["libs/shared/src/index.ts"]
    assert lib["projectRoot"] == "libs/shared" and lib.get("project") is None
    assert "project" not in records["tools/outside.ts"]
    assert "projectRoot" not in records["tools/outside.ts"]


def test_legacy_workspace_map_assigns_files(tmp_path: Path) -> None:
    repo = _write(tmp_path, {"nx.json": {}, "workspace.json": {"projects": {"web": {"root": "apps/web"}}},
                             "apps/web/src/main.ts": "export const web = true;\n"})
    records = {r.path: r for r in iter_file_records(repo)}
    assert records["apps/web/src/main.ts"].project == "web"
    assert records["apps/web/src/main.ts"].projectRoot == "apps/web"


def test_files_of_a_non_nx_repo_are_unchanged(tmp_path: Path) -> None:
    repo = _write(tmp_path, {"legacy-api/project.json": {"name": "api", "frameworks": {}},
                             "legacy-api/Program.cs": "class Program {}\n"})
    records = {r.path: r for r in iter_file_records(repo)}
    assert records["legacy-api/Program.cs"].project is None
    assert records["legacy-api/Program.cs"].projectRoot is None
    assert records["legacy-api/project.json"].language == "structured-json"
