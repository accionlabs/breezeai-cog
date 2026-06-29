"""Executor tests: parallel parsing yields the same records as sequential."""

from __future__ import annotations

from breezeai_cog.config import Settings
from breezeai_cog.core import executor, pipeline


def _make_repo(root) -> None:
    for i in range(6):
        (root / f"m{i}.py").write_text(
            f"import os\n\nclass C{i}:\n    def method(self):\n        return {i}\n\n"
            f"def fn{i}(a):\n    return a + {i}\n"
        )


def _fingerprint(entries, repo, settings) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for _lang, rec in executor.parse_entries(entries, repo, settings):
        out[rec.path] = sorted(f.name for f in rec.functions)
    return out


def test_parallel_matches_sequential(tmp_path) -> None:
    _make_repo(tmp_path)
    seq_settings = Settings(_env_file=None, jobs=1)
    par_settings = Settings(_env_file=None, jobs=4)
    entries = list(pipeline._scan_entries(tmp_path, seq_settings))
    assert len(entries) == 6

    sequential = _fingerprint(entries, tmp_path, seq_settings)
    parallel = _fingerprint(entries, tmp_path, par_settings)

    assert sequential == parallel
    assert set(sequential) == {f"m{i}.py" for i in range(6)}
    assert sequential["m0.py"] == ["fn0", "method"]
