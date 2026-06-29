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
