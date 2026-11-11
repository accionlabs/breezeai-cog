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
