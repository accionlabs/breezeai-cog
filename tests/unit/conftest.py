"""Shared fixtures for unit tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import os

import pytest

from breezeai_cog import iter_file_records
from breezeai_cog.parsers.contract import validate
from breezeai_cog.schemas import Statement


def _windows_spelled(path: Path) -> Path:
    """``path``, but ``str()`` keeps a ``./`` segment before the file name: a real path whose
    string spelling no longer ends with its ``/``-separated repo-relative path — as on Windows,
    where ``str()`` uses ``\\``. Reproduces that mismatch on any OS."""

    class _Spelled(type(path)):  # type: ignore[valid-type,misc]
        def __str__(self) -> str:
            plain = super().__str__()
            return f"{os.path.dirname(plain)}/./{os.path.basename(plain)}"

    return _Spelled(path)


@pytest.fixture
def windows_spelled() -> Callable[[Path], Path]:
    return _windows_spelled


@pytest.fixture
def analyze_files(tmp_path: Path) -> Callable[..., list[Statement]]:
    """Write ``{relative path: source}`` into a fresh repo, run the whole pipeline (scan, index,
    parse, additive detectors) and return every statement. For role-table tests, where a binding
    often depends on wiring in another file.

    Every record is checked against the entry-point contract (``parsers/contract.py``): an
    error (a wrong value) fails the test; a gap (a missing value) does not."""

    def run(files: dict[str, str], *, templates: bool = False) -> list[Statement]:
        repo = tmp_path / "repo"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        records = list(iter_file_records(repo, capture_templates=templates))
        errors = [v for rec in records for v in validate(rec) if v.severity == "error"]
        assert not errors, f"entry-point contract errors: {errors}"
        return [s for rec in records for s in rec.statements]

    return run


@pytest.fixture
def capture_routes(tmp_path: Path) -> Callable[..., dict[str, list[dict[str, object]]]]:
    """Write ``{relative path: source}`` into a fresh repo and capture it with ``analyze_repo`` at
    the given ``jobs`` — the index is built in the parent and read in the workers, which a
    hand-built index cannot show. Returns file path → its ``route`` statements as dicts."""

    def run(files: dict[str, str], jobs: int) -> dict[str, list[dict[str, object]]]:
        import gzip
        import json

        from breezeai_cog import analyze_repo

        repo = tmp_path / f"repo{jobs}"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        result = analyze_repo(repo, capture_statements=True, out=tmp_path / f"out{jobs}", jobs=jobs)
        out: dict[str, list[dict[str, object]]] = {}
        for line in gzip.open(result.out_path, "rt", encoding="utf-8"):
            rec = json.loads(line)
            routes = [s for s in rec.get("statements", []) if s.get("semanticType") == "route"]
            if routes:
                out[rec["path"]] = routes
        return out

    return run
