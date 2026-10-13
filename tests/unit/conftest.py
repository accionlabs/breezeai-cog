"""Shared fixtures for unit tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from breezeai_cog import iter_file_records
from breezeai_cog.schemas import Statement


@pytest.fixture
def analyze_files(tmp_path: Path) -> Callable[..., list[Statement]]:
    """Write ``{relative path: source}`` into a fresh repo, run the whole pipeline (scan, index,
    parse, additive detectors) and return every statement. For role-table tests, where a binding
    often depends on wiring in another file."""

    def run(files: dict[str, str], *, templates: bool = False) -> list[Statement]:
        repo = tmp_path / "repo"
        for rel, src in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
        return [
            s
            for rec in iter_file_records(repo, capture_templates=templates)
            for s in rec.statements
        ]

    return run
