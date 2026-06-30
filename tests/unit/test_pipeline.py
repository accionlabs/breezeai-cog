"""End-to-end pipeline / public-API tests on a tiny temp repo."""

from __future__ import annotations

import gzip
import json

import breezeai_cog
from breezeai_cog import analyze_repo, capabilities, iter_file_records


def _make_repo(root) -> None:
    (root / "app.py").write_text("import os\n\nclass A:\n    def m(self):\n        return 1\n\ndef top():\n    return 2\n")
    (root / "util.py").write_text("def helper():\n    return 3\n")
    (root / ".venv").mkdir()
    (root / ".venv" / "junk.py").write_text("should = 'be ignored'\n")  # pruned by builtin
    (root / "test_app.py").write_text("def test_x():\n    assert True\n")  # excluded (test_*.py)


def test_analyze_repo_writes_metadata_first(tmp_path) -> None:
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _make_repo(repo)

    result = analyze_repo(repo)
    assert result.out_path is not None and result.out_path.exists()

    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    records = [json.loads(line) for line in lines]

    assert records[0]["__type"] == "projectMetaData"
    meta = records[0]
    assert meta["totalFiles"] == 2  # app.py + util.py (.venv pruned, test_ excluded)
    assert meta["totalClasses"] == 1
    assert meta["totalFunctions"] == 3  # A.m, top, helper
    assert meta["analyzedLanguages"] == ["python"]
    assert meta["toolVersion"] == breezeai_cog.__version__

    paths = sorted(r["path"] for r in records[1:])
    assert paths == ["app.py", "util.py"]


def test_iter_file_records(tmp_path) -> None:
    repo = tmp_path / "r2"
    repo.mkdir()
    _make_repo(repo)
    records = list(iter_file_records(repo))
    assert sorted(r.path for r in records) == ["app.py", "util.py"]
    assert all(isinstance(r.loc, int) for r in records)


def test_capabilities() -> None:
    caps = capabilities()
    assert "python" in caps["languages"]
    assert ".py" in caps["extensions"]
    assert caps["schemaVersion"] == "2.0"


def test_run_reports_progress(tmp_path) -> None:
    """pipeline.run forwards live (done, total) progress as files complete."""
    from breezeai_cog.config import Settings
    from breezeai_cog.core import pipeline
    from breezeai_cog.emit.sinks import MemorySink

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(5):
        (repo / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n")

    seen: list[tuple[int, int]] = []
    pipeline.run(repo, Settings(jobs=1), MemorySink(), progress=lambda d, t: seen.append((d, t)))

    assert seen[0] == (0, 5)                       # total set up front
    assert seen[-1] == (5, 5)                       # ends at total
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)  # monotonic
    assert all(t == 5 for _, t in seen)             # total stable
