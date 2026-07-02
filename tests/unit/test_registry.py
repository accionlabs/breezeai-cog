"""Registry: single-parser selection (claims + priority), base resolution,
capabilities, the schema-version gate, and filename matching for config files."""

from __future__ import annotations

import pytest

from breezeai_cog.core import registry
from breezeai_cog.errors import RegistryError
from breezeai_cog.parsers.base import BaseParser, ParseContext
from breezeai_cog.schemas import SCHEMA_VERSION, FileRecord


@pytest.fixture(autouse=True)
def _clean_registry():
    registry.clear()
    yield
    registry.clear()


class FakeLang(BaseParser):
    name = "fake"
    extensions = (".fk",)
    statement_types = ["if_statement"]
    frameworks = ["fwA"]

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        return FileRecord(id=ctx.path, path=ctx.path, type="code", language="fake", loc=1)


class FakeFramework(FakeLang):
    name = "fake-fw"
    priority = 10
    frameworks = ["fw"]

    def claims(self, path: str, source: bytes) -> bool:
        return b"FRAMEWORK" in source


class ConfigLang(BaseParser):
    name = "config"
    extensions = (".json", "Dockerfile", ".env")

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        return FileRecord(id=ctx.path, path=ctx.path, type="config", language="config", loc=1)


def test_select_base_when_no_framework() -> None:
    registry.register(FakeLang())
    assert registry.select("a.fk", b"plain").name == "fake"
    assert registry.base_parser_for("a.fk").name == "fake"


def test_select_no_match() -> None:
    registry.register(FakeLang())
    assert registry.select("a.py", b"") is None


def test_framework_selected_only_when_it_claims() -> None:
    registry.register(FakeLang())
    registry.register(FakeFramework())
    # framework signature present -> higher-priority framework parser wins
    assert registry.select("a.fk", b"uses FRAMEWORK here").name == "fake-fw"
    # absent -> falls back to the base language parser (one parser per file)
    assert registry.select("a.fk", b"plain code").name == "fake"
    assert registry.base_parser_for("a.fk").name == "fake"  # base = priority 0


def test_capabilities_aggregate() -> None:
    registry.register(FakeLang())
    registry.register(FakeFramework())
    caps = registry.capabilities()
    assert caps["schemaVersion"] == SCHEMA_VERSION
    assert set(caps["languages"]) == {"fake", "fake-fw"}
    assert "fw" in caps["frameworks"]
    assert caps["statementTypes"] == ["if_statement"]


def test_schema_version_gate() -> None:
    class Stale(FakeLang):
        schema_version = "0.0-old"

    with pytest.raises(RegistryError):
        registry.register(Stale())


def test_config_filename_matching() -> None:
    registry.register(ConfigLang())
    assert registry.select("Dockerfile", b"") is not None
    assert registry.select("config/app.json", b"") is not None
    assert registry.select("svc/.env", b"") is not None
    assert registry.select("main.go", b"") is None


def test_specific_framework_outranks_express() -> None:
    # A NestJS/LoopBack controller that also imports express must be parsed by the
    # specific framework, not the base Express parser (both claim; priority breaks the tie).
    registry.discover_builtin()
    nest = b"import { Controller, Get } from '@nestjs/common';\nimport { Request } from 'express';\n"
    assert registry.select("orders.controller.ts", nest).name == "typescript-nestjs"
    lb = b"import { get } from '@loopback/rest';\nimport { Request } from 'express';\n"
    assert registry.select("order.controller.ts", lb).name == "typescript-loopback"
    # a pure express file still selects the express parser
    assert registry.select("app.ts", b"import express from 'express';\n").name == "typescript-express"
