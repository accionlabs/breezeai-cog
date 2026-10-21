"""Executor tests: parallel parsing yields the same records as sequential."""

from __future__ import annotations

import os

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


def _make_alias_repo(root) -> None:
    """A tsconfig path alias resolves only through the TypeScript index."""
    (root / "tsconfig.json").write_text('{"compilerOptions": {"baseUrl": ".", "paths": {"@app/*": ["src/*"]}}}')
    (root / "src").mkdir()
    (root / "src" / "util.ts").write_text("export function add(a: number) { return a + 1; }\n")
    for i in range(4):
        (root / "src" / f"m{i}.ts").write_text(
            f"import {{ add }} from '@app/util';\nexport const v{i} = add({i});\n")


def _imports(repo, jobs: int) -> dict[str, list[str]]:
    settings = Settings(_env_file=None, jobs=jobs)
    entries = list(pipeline._scan_entries(repo, settings))
    indexes = pipeline._build_indexes(repo, entries, jobs)
    return {rec.path: rec.importFiles
            for _lang, rec in executor.parse_entries(entries, repo, settings, indexes)}


def test_parallel_workers_get_the_indexes_from_a_temp_file(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_alias_repo(repo)
    made: list[str] = []
    real_mkdtemp = executor.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real_mkdtemp(dir=tmp_path, **kw))
        return made[-1]

    monkeypatch.setattr(executor.tempfile, "mkdtemp", mkdtemp)
    parallel = _imports(repo, jobs=2)

    assert parallel["src/m0.ts"] == ["src/util.ts"]  # resolved through the index in a worker
    assert parallel == _imports(repo, jobs=1)
    assert len(made) == 1 and not os.path.exists(made[0])  # removed after the pool shut down


def test_indexes_stay_in_initargs_without_a_temp_dir(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_alias_repo(repo)

    def no_temp(**kw):
        raise OSError("read-only file system")

    monkeypatch.setattr(executor.tempfile, "mkdtemp", no_temp)
    assert _imports(repo, jobs=2)["src/m0.ts"] == ["src/util.ts"]
