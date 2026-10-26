"""Additive detector registry (parsers/additive.py): discovery, validation and runner rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.errors import RegistryError
from breezeai_cog.parsers import additive
from breezeai_cog.parsers.additive import (
    HOOKED_LANGUAGES,
    INDEX_HOOKED_LANGUAGES,
    Detection,
    Detector,
    FactSpec,
    Gate,
    all_detectors,
    index_fact,
    register_detector,
    register_fact,
    run_additive,
)
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.emits import Emit
from breezeai_cog.schemas import FileRecord

PARSERS_DIR = Path(additive.__file__).parent


@pytest.fixture
def isolated(monkeypatch):
    """Run against a copy of the registry so test registrations never leak."""
    additive.discover_detectors()
    snapshot = {lang: list(ds) for lang, ds in additive._DETECTORS.items()}
    monkeypatch.setattr(additive, "_DETECTORS", snapshot)
    monkeypatch.setattr(additive, "_FACTS", {k: list(v) for k, v in additive._FACTS.items()})
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
        "csharp": ["lambda-handlers", "lucene", "hotchocolate-registered-roots",
                   "generic-handlers", "hangfire", "quartz"],
        "java": ["quartz"],
        "python": ["celery"],
        "php": ["wordpress-hooks"],
        "scala": ["akka-events", "spark"],
        "typescript": ["express", "fastify", "aws-events", "sdk-calls", "graphql-client",
                       "graphql-resolver-modules", "vue-routes",
                       "nextjs-pages", "vue-navigation", "angular-navigation",
                       "react-navigation"],
    }


@pytest.mark.parametrize(
    "language, parser_file", [(lang, f"{lang}/parser.py") for lang in sorted(HOOKED_LANGUAGES)]
)
def test_hooked_language_parsers_call_the_runner(language: str, parser_file: str) -> None:
    source = (PARSERS_DIR / parser_file).read_text()
    assert "run_additive(" in source and f'"{language}"' in source


def test_unhooked_language_rejected(isolated) -> None:
    with pytest.raises(RegistryError, match="no additive hook"):
        register_detector(Detector(name="x", gate=Gate("c#", order=999), detect=lambda dc: None))


def test_duplicate_order_rejected(isolated) -> None:
    with pytest.raises(RegistryError, match="order 10 already used"):
        register_detector(Detector(name="x", gate=Gate("csharp", order=10), detect=lambda dc: None))


def test_reregistering_same_name_is_idempotent(isolated) -> None:
    before = [d.name for d in isolated["csharp"]]
    register_detector(Detector(name="lucene", gate=Gate("csharp", order=999), detect=lambda dc: None))
    assert [d.name for d in isolated["csharp"]] == before


def _probe(isolated, **kw) -> list[str]:
    calls: list[str] = []
    isolated["typescript"] = [
        Detector(name="probe", gate=Gate("typescript", order=1, **kw),
                 detect=lambda dc: calls.append(dc.path))
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
        Detector(name="probe", gate=Gate("typescript", order=1),
                 detect=lambda dc: Detection([], label="kafka"))
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
def test_gate_admits() -> None:
    gate = Gate("typescript", order=1, guard=b"kafkajs", skip_fixtures=True)
    assert gate.admits(b"import 'kafkajs'", is_fixture=False)
    assert not gate.admits(b"plain", is_fixture=False)
    assert not gate.admits(b"import 'kafkajs'", is_fixture=True)
    assert Gate("typescript", order=1).admits(b"plain", is_fixture=True)


def test_detection_is_applied_and_labels_the_file(isolated) -> None:
    from breezeai_cog.parsers.treesitter import parse_source

    source = b"app.get('/x', h);\n"
    root = parse_source("typescript", source, 0).root_node
    call = root.named_children[0].named_children[0]

    def detect(dc):
        return Detection([Emit(call, {"semanticType": "route", "endpoint": "/x"})], label="express")

    isolated["typescript"] = [Detector(name="probe", gate=Gate("typescript", order=1), detect=detect)]
    record = _record()
    run_additive("typescript", root, _ctx(source), record, is_fixture=False)
    [s] = record.statements
    assert (s.id, s.semanticType, s.endpoint, s.text) == ("a.ts:1:0", "route", "/x", "app.get('/x', h)")
    assert record.framework == "express"


# ---- facts: built from other files while the language index is built ----------------------------

def _collect_marked(root, source: bytes, rel: str, repo_root) -> str:
    return rel


def _resolve_marked(values: list, index) -> list:
    # A fact may use the language index: keep only files that also declare a type.
    declaring = {f for files in index.types.values() for f in files}
    return sorted(v for v in values if v in declaring)


def _marked(**overrides) -> FactSpec:
    fields = dict(name="test.marked", language="csharp", gate=(b"// MARK",),
                  collect=_collect_marked, resolve=_resolve_marked)
    fields.update(overrides)
    return FactSpec(**fields)


@pytest.mark.parametrize("language", sorted(INDEX_HOOKED_LANGUAGES))
def test_index_hooked_languages_call_the_collector(language: str) -> None:
    # The index is built in imports.py (index.py for Python).
    source = "".join((PARSERS_DIR / language / f).read_text()
                     for f in ("imports.py", "index.py") if (PARSERS_DIR / language / f).exists())
    assert f'collect_additive("{language}"' in source and f'resolve_additive("{language}"' in source


def test_fact_for_an_unhooked_language_rejected(isolated) -> None:
    with pytest.raises(RegistryError, match="no index hook"):
        register_fact(_marked(language="kotlin"))


def test_fact_needs_a_gate(isolated) -> None:
    with pytest.raises(RegistryError, match="needs a gate"):
        register_fact(_marked(gate=()))


@pytest.mark.parametrize("jobs", [1, 2])
def test_fact_is_resolved_on_the_index(isolated, tmp_path, jobs) -> None:
    from breezeai_cog.parsers.csharp.imports import build_csharp_index

    register_fact(_marked())
    files = {
        "a/Marked.cs": b"// MARK\nnamespace A { class Marked {} }",
        "b/Plain.cs": b"namespace B { class Plain {} }",          # gate does not match
        "c/MarkedNoType.cs": b"// MARK\n",                         # resolve drops it
    }
    paths = []
    for rel, src in files.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(src)
        paths.append(f)
    index = build_csharp_index(tmp_path, paths, jobs)
    # Collected behind the gate, resolved once against the full index, the same for any --jobs.
    assert index_fact(index, "test.marked") == ["a/Marked.cs"]



@pytest.mark.parametrize("jobs", [1, 2])
def test_typescript_fact_is_resolved_on_the_index(isolated, tmp_path, jobs) -> None:
    from breezeai_cog.parsers.typescript.imports import build_ts_index

    register_fact(_marked(language="typescript", resolve=lambda values, index: sorted(values)))
    paths = []
    for rel, src in {"a.ts": b"// MARK\nexport const a = 1;", "b.ts": b"export const b = 2;"}.items():
        f = tmp_path / rel
        f.write_bytes(src)
        paths.append(f)
    index = build_ts_index(tmp_path, paths, jobs)
    assert index_fact(index, "test.marked") == ["a.ts"]



def test_fact_reads_files_another_parser_owns(isolated, tmp_path) -> None:
    from breezeai_cog.parsers.csharp.imports import build_csharp_index

    register_fact(_marked(
        name="test.hosts", gate=(b"ServiceHost",), sources=(".svc",),
        collect=lambda root, source, rel, repo_root: (rel, root is None),
        resolve=lambda values, index: sorted(values),
    ))
    (tmp_path / "Api.svc").write_bytes(b'<%@ ServiceHost Service="A.Api" %>')
    (tmp_path / "Api.svc.cs").write_bytes(b"namespace A { class Api {} }")
    files = [tmp_path / "Api.svc.cs", tmp_path / "Api.svc"]
    assert additive.split_fact_sources("csharp", files) == ([files[0]], [files[1]])
    index = build_csharp_index(tmp_path, files, 1)
    # The .svc reaches only the fact (no grammar → no tree); C# indexing sees only the .cs file.
    assert index_fact(index, "test.hosts") == [("Api.svc", True)]
    assert {f for fs in index.types.values() for f in fs} == {"Api.svc.cs"}

def test_index_fact_tolerates_a_missing_index() -> None:
    assert index_fact(None, "anything") is None
