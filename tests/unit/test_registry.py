"""Tests for the parser registry: matching, composition, capabilities, the
schema-version gate, and filename matching for extension-less config files."""

from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.core import registry
from breezeai_cog.errors import RegistryError
from breezeai_cog.parsers.base import BaseParser, ParseContext
from breezeai_cog.schemas import SCHEMA_VERSION, Class, FileRecord, Function


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


def _ctx(path: str) -> ParseContext:
    return ParseContext(path=path, abs_path=Path("/repo") / path, source=b"", repo_root=Path("/repo"))


class FakeA(BaseParser):
    name = "fake"
    extensions = (".fk",)
    statement_types = ["if_statement"]
    frameworks = ["fwA"]

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        fn = Function(id=f"{ctx.path}#a@1", parentId=ctx.path, name="a", type="function",
                      startLine=1, endLine=2)
        return FileRecord(id=ctx.path, path=ctx.path, type="code", language="fake", loc=10,
                          importFiles=["x.fk"], functions=[fn])


class FakeB(BaseParser):
    name = "fakeB"
    extensions = (".fk",)
    statement_types = ["return_statement"]
    frameworks = ["fwB"]

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        fn = Function(id=f"{ctx.path}#b@3", parentId=ctx.path, name="b", type="function",
                      startLine=3, endLine=4)
        cls = Class(id=f"{ctx.path}#C", parentId=ctx.path, name="C", type="class",
                    startLine=5, endLine=9)
        return FileRecord(id=ctx.path, path=ctx.path, type="code", language="fakeB", loc=10,
                          importFiles=["y.fk"], classes=[cls], functions=[fn])


class ConfigParser(BaseParser):
    name = "config"
    extensions = (".json", "Dockerfile", ".env")

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        return FileRecord(id=ctx.path, path=ctx.path, type="config", language="config", loc=1)


def test_single_match_returns_that_parser() -> None:
    registry.register(FakeA())
    p = registry.parser_for("a.fk")
    assert isinstance(p, FakeA)


def test_no_match_returns_none() -> None:
    registry.register(FakeA())
    assert registry.parser_for("a.py") is None


def test_composition_merges_records() -> None:
    registry.register(FakeA())
    registry.register(FakeB())
    p = registry.parser_for("a.fk")
    assert p.name == "fake+fakeB"  # CompositeParser
    rec = p.parse_file(_ctx("a.fk"))
    assert [f.name for f in rec.functions] == ["a", "b"]
    assert [c.name for c in rec.classes] == ["C"]
    assert rec.importFiles == ["x.fk", "y.fk"]  # unioned, order-preserving


def test_capabilities_aggregate() -> None:
    registry.register(FakeA())
    registry.register(FakeB())
    caps = registry.capabilities()
    assert caps["schemaVersion"] == SCHEMA_VERSION
    assert caps["languages"] == ["fake", "fakeB"]
    assert caps["frameworks"] == ["fwA", "fwB"]
    assert caps["statementTypes"] == ["if_statement", "return_statement"]


def test_schema_version_gate() -> None:
    class Stale(FakeA):
        schema_version = "0.0-old"

    with pytest.raises(RegistryError):
        registry.register(Stale())


class FrameworkFk(BaseParser):
    name = "fake-fw"
    extensions = (".fk",)
    overrides = ("fake",)  # supersede FakeA

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        return FileRecord(id=ctx.path, path=ctx.path, type="code", language="fake-fw", loc=1)


def test_override_skips_base_no_composite() -> None:
    registry.register(FakeA())  # name "fake"
    registry.register(FrameworkFk())  # overrides ("fake",)
    p = registry.parser_for("a.fk")
    assert isinstance(p, FrameworkFk)  # not a CompositeParser; base skipped
    assert registry.parsers_for("a.fk") == [registry.registered()[1]]


def test_override_only_applies_when_overrider_matches() -> None:
    class OtherExt(FrameworkFk):
        extensions = (".other",)  # does NOT claim .fk

    registry.register(FakeA())
    registry.register(OtherExt())
    # OtherExt doesn't match .fk, so its override of "fake" has no effect here
    assert isinstance(registry.parser_for("a.fk"), FakeA)


def test_config_filename_matching() -> None:
    registry.register(ConfigParser())
    assert registry.parser_for("Dockerfile") is not None
    assert registry.parser_for("config/app.json") is not None
    assert registry.parser_for("svc/.env") is not None
    assert registry.parser_for("main.go") is None
