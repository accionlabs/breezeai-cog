"""CLI smoke tests via Typer's CliRunner."""

from __future__ import annotations

import gzip
import json

import pytest
from typer.testing import CliRunner

from breezeai_cog import __version__
from breezeai_cog.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_log_files(monkeypatch):
    monkeypatch.setenv("BREEZEAI_COG_LOG_TO_FILE", "false")  # don't create ./logs in tests


def test_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_capabilities() -> None:
    result = runner.invoke(app, ["capabilities"])
    assert result.exit_code == 0
    caps = json.loads(result.stdout)
    assert "python" in caps["languages"] and caps["schemaVersion"] == "2.0"


def test_repo_to_json_tree(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("def f():\n    return 1\n")
    (repo / "b.py").write_text("class B:\n    pass\n")
    out_dir = tmp_path / "results"  # --out is a directory; filename is derived

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    out = out_dir / "repo-project-analysis.ndjson.gz"
    assert out.exists()
    records = [json.loads(line) for line in gzip.open(out, "rt", encoding="utf-8").read().splitlines()]
    assert records[0]["__type"] == "projectMetaData"
    assert records[0]["totalFiles"] == 2


def test_repo_to_json_tree_batch(tmp_path) -> None:
    """--batch analyzes each immediate subdirectory as its own project."""
    workspace = tmp_path / "workspace"
    (workspace / "proj-a").mkdir(parents=True)
    (workspace / "proj-b").mkdir()
    (workspace / ".hidden").mkdir()  # dot dirs are skipped
    (workspace / "proj-a" / "a.py").write_text("def f():\n    return 1\n")
    (workspace / "proj-b" / "b.py").write_text("class B:\n    pass\n")
    (workspace / ".hidden" / "h.py").write_text("def h():\n    return 0\n")
    (workspace / "loose.py").write_text("x = 1\n")  # loose files are ignored
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app,
        ["repo-to-json-tree", "--repo", str(workspace), "--batch", "--out", str(out_dir), "--jobs", "1"],
    )
    assert result.exit_code == 0, result.output

    a = out_dir / "proj-a-project-analysis.ndjson.gz"
    b = out_dir / "proj-b-project-analysis.ndjson.gz"
    assert a.exists() and b.exists()
    # exactly two projects — .hidden and loose.py produce no output
    assert sorted(p.name for p in out_dir.glob("*.ndjson.gz")) == [
        "proj-a-project-analysis.ndjson.gz",
        "proj-b-project-analysis.ndjson.gz",
    ]
    meta_a = json.loads(gzip.open(a, "rt", encoding="utf-8").readline())
    assert meta_a["__type"] == "projectMetaData"
    assert meta_a["totalFiles"] == 1


def test_repo_to_json_tree_batch_empty(tmp_path) -> None:
    """--batch on a folder with no subdirectories exits non-zero."""
    workspace = tmp_path / "empty"
    workspace.mkdir()
    result = runner.invoke(app, ["repo-to-json-tree", "--repo", str(workspace), "--batch"])
    assert result.exit_code == 1


def test_repo_to_json_tree_no_output_when_nothing_parsed(tmp_path) -> None:
    """A repo with no parseable source files writes no .ndjson.gz."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.txt").write_text("just prose, no code\n")  # not a supported source file
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    assert not (out_dir / "repo-project-analysis.ndjson.gz").exists()
    assert list(out_dir.glob("*.ndjson.gz")) == []
    assert "no ndjson written" in result.output


def test_repo_to_json_tree_no_output_for_trivial_config_only(tmp_path) -> None:
    """A folder whose only file is a trivial config (no code, no real signal) writes nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Title\n\njust docs\n")  # classified config, no content
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    assert list(out_dir.glob("*.ndjson.gz")) == []
    assert "no ndjson written" in result.output


def test_repo_to_json_tree_writes_config_repo_with_dependencies(tmp_path) -> None:
    """A config-only repo that carries real signal (dependencies) still produces output."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"name": "x", "dependencies": {"left-pad": "^1.0.0"}}\n'
    )
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app, ["repo-to-json-tree", "--repo", str(repo), "--out", str(out_dir), "--jobs", "1"]
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "repo-project-analysis.ndjson.gz").exists()


def test_repo_to_json_tree_batch_skips_empty_projects(tmp_path) -> None:
    """--batch emits files only for subdirs that actually parse to records."""
    workspace = tmp_path / "workspace"
    (workspace / "code").mkdir(parents=True)
    (workspace / "docs").mkdir()
    (workspace / "code" / "a.py").write_text("def f():\n    return 1\n")
    (workspace / "docs" / "notes.txt").write_text("no code here\n")
    out_dir = tmp_path / "results"

    result = runner.invoke(
        app,
        ["repo-to-json-tree", "--repo", str(workspace), "--batch", "--out", str(out_dir), "--jobs", "1"],
    )
    assert result.exit_code == 0, result.output
    assert sorted(p.name for p in out_dir.glob("*.ndjson.gz")) == [
        "code-project-analysis.ndjson.gz",
    ]
