"""Additive detector registry (parsers/additive.py): discovery, validation and runner rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.errors import RegistryError
from breezeai_cog.parsers import additive
from breezeai_cog.parsers.additive import (
    HOOKED_LANGUAGES,
    Detector,
    all_detectors,
    register_detector,
    run_additive,
)
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.schemas import FileRecord

PARSERS_DIR = Path(additive.__file__).parent


@pytest.fixture
def isolated(monkeypatch):
    """Run against a copy of the registry so test registrations never leak."""
    additive.discover_detectors()
    snapshot = {lang: list(ds) for lang, ds in additive._DETECTORS.items()}
    monkeypatch.setattr(additive, "_DETECTORS", snapshot)
    return snapshot


def _ctx(source: bytes, *, capture: bool = True) -> ParseContext:
    return ParseContext(
        path="a.ts", abs_path=Path("a.ts"), source=source, repo_root=Path("."),
        capture_statements=capture,
    )


def _record() -> FileRecord:
    return FileRecord(id="f", path="a.ts", type="code", language="typescript", loc=1)


def test_inventory_in_run_order() -> None:
    """Snapshot: a new or reordered detector must show up here. Order is part of the output
    (statement-id disambiguation), so a change to it is a deliberate, reviewed change."""
    inventory = {lang: [d.name for d in ds] for lang, ds in all_detectors().items()}
    assert inventory == {
        "csharp": ["lambda-handlers", "lucene"],
        "scala": ["akka-events", "spark"],
        "typescript": ["express", "aws-events", "sdk-calls", "graphql-client", "vue-routes"],
    }


@pytest.mark.parametrize(
    "language, parser_file", [(lang, f"{lang}/parser.py") for lang in sorted(HOOKED_LANGUAGES)]
)
def test_hooked_language_parsers_call_the_runner(language: str, parser_file: str) -> None:
    source = (PARSERS_DIR / parser_file).read_text()
    assert "run_additive(" in source and f'"{language}"' in source


def test_unhooked_language_rejected(isolated) -> None:
    with pytest.raises(RegistryError, match="no additive hook"):
        register_detector(Detector(name="x", language="c#", order=999, run=lambda dc: None))


def test_duplicate_order_rejected(isolated) -> None:
    with pytest.raises(RegistryError, match="order 10 already used"):
        register_detector(Detector(name="x", language="csharp", order=10, run=lambda dc: None))


def test_reregistering_same_name_is_idempotent(isolated) -> None:
    before = [d.name for d in isolated["csharp"]]
    register_detector(Detector(name="lucene", language="csharp", order=999, run=lambda dc: None))
    assert [d.name for d in isolated["csharp"]] == before


def _probe(isolated, **kw) -> list[str]:
    calls: list[str] = []
    isolated["typescript"] = [
        Detector(name="probe", language="typescript", order=1,
                 run=lambda dc: calls.append(dc.path), **kw)
    ]
    return calls


def test_runner_skips_without_statement_capture(isolated) -> None:
    calls = _probe(isolated)
    run_additive("typescript", None, _ctx(b"x", capture=False), _record(), is_fixture=False)
    assert calls == []


def test_runner_skips_fixtures_only_when_asked(isolated) -> None:
    calls = _probe(isolated, skip_fixtures=True)
    run_additive("typescript", None, _ctx(b"x"), _record(), is_fixture=True)
    assert calls == []
    calls = _probe(isolated)
    run_additive("typescript", None, _ctx(b"x"), _record(), is_fixture=True)
    assert calls == ["a.ts"]


def test_runner_applies_byte_guard(isolated) -> None:
    calls = _probe(isolated, guard=b"kafkajs")
    run_additive("typescript", None, _ctx(b"plain"), _record(), is_fixture=False)
    assert calls == []
    run_additive("typescript", None, _ctx(b"import 'kafkajs'"), _record(), is_fixture=False)
    assert calls == ["a.ts"]


def test_label_only_fills_an_empty_framework(isolated) -> None:
    isolated["typescript"] = [
        Detector(name="probe", language="typescript", order=1, run=lambda dc: "kafka")
    ]
    record = _record()
    run_additive("typescript", None, _ctx(b"x"), record, is_fixture=False)
    assert record.framework == "kafka"
    record = _record()
    record.framework = "nestjs"
    run_additive("typescript", None, _ctx(b"x"), record, is_fixture=False)
    assert record.framework == "nestjs"


def test_detectors_run_in_spawned_workers(tmp_path) -> None:
    """Workers are spawned, so each must discover detectors itself; a pool run must match
    the serial run (an Express route only appears if the worker found the detector)."""
    from breezeai_cog.config import Settings
    from breezeai_cog.core import pipeline
    from breezeai_cog.emit.sinks import MemorySink

    repo = tmp_path / "repo"
    repo.mkdir()
    for i in range(4):
        (repo / f"r{i}.js").write_text(
            "const express = require('express');\n"
            "const app = express();\n"
            f"app.get('/items/{i}', (req, res) => res.send('ok'));\n"
        )

    def routes(jobs: int) -> list[tuple[str, str | None]]:
        sink = MemorySink()
        pipeline.run(repo, Settings(jobs=jobs), sink)
        return sorted(
            (r.path, s.endpoint)
            for r in sink.records for s in r.statements if s.semanticType == "route"
        )

    serial = routes(1)
    assert len(serial) == 4
    assert routes(2) == serial
