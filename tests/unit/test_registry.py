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


def test_select_passes_parse_timeout_to_every_claim() -> None:
    timeouts: list[int] = []

    class TimeoutLang(FakeLang):
        def claims(self, path: str, source: bytes, parse_timeout_micros: int = 0) -> bool:
            timeouts.append(parse_timeout_micros)
            return True

    class TimeoutFramework(TimeoutLang):
        name = "timeout-fw"
        priority = 10

        def claims(self, path: str, source: bytes, parse_timeout_micros: int = 0) -> bool:
            timeouts.append(parse_timeout_micros)
            return True

    registry.register(TimeoutLang())
    registry.register(TimeoutFramework())

    selected = registry.select("a.fk", b"source", parse_timeout_micros=1234)

    assert selected is not None and selected.name == "timeout-fw"
    assert timeouts == [1234, 1234]


def test_select_supports_legacy_two_argument_claims() -> None:
    registry.register(FakeLang())
    registry.register(FakeFramework())

    selected = registry.select("a.fk", b"FRAMEWORK", parse_timeout_micros=1234)

    assert selected is not None and selected.name == "fake-fw"


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


@pytest.mark.parametrize(
    "path",
    [
        "tasks/release.rake",
        "rubocop.gemspec",
        "Gemfile",
        "Rakefile",
    ],
)
def test_ruby_project_file_matching(path: str) -> None:
    registry.discover_builtin()

    parser = registry.base_parser_for(path)

    assert parser is not None and parser.name == "ruby"
    assert registry.select(path, b"") is parser


@pytest.mark.parametrize(
    "path",
    [
        "Gemfile.local",
        "custom-extensionless-file",
        "exe/other",
        "other/exe/rubocop",
        "bin/console",
    ],
)
def test_ruby_project_file_matching_is_narrow(path: str) -> None:
    registry.discover_builtin()

    assert registry.base_parser_for(path) is None
    assert registry.select(path, b"") is None


def test_specific_framework_outranks_express() -> None:
    # A NestJS/LoopBack controller that also imports express must be parsed by the
    # specific framework, not the base Express parser (both claim; priority breaks the tie).
    registry.discover_builtin()
    nest = b"import { Controller, Get } from '@nestjs/common';\nimport { Request } from 'express';\n"
    assert registry.select("orders.controller.ts", nest).name == "typescript-nestjs"
    lb = b"import { get } from '@loopback/rest';\nimport { Request } from 'express';\n"
    assert registry.select("order.controller.ts", lb).name == "typescript-loopback"
    # a pure express file is owned by the base parser now — Express is an additive detector
    # (its routes are captured in extract), not a selecting parser.
    assert registry.select("app.ts", b"import express from 'express';\n").name == "typescript"


def test_rails_outranks_grape_when_both_claim_ruby_file() -> None:
    registry.discover_builtin()
    source = b'''require "rails"
class UsersAPI < Grape::API
  get "/users" do
  end
end
Rails.application.routes.draw do
  get "/health", to: "health#show"
end
'''

    assert registry.select("config/routes.rb", source).name == "ruby-rails"


def test_ruby_framework_claim_uses_registry_parse_timeout(monkeypatch) -> None:
    from breezeai_cog.parsers import ruby_rails
    from breezeai_cog.parsers.ruby.parser import RubyParser

    parse_timeouts: list[int] = []
    original_parse_source = ruby_rails.parser.parse_source

    def recording_parse_source(language: str, source: bytes, timeout_micros: int = 0):
        parse_timeouts.append(timeout_micros)
        return original_parse_source(language, source, timeout_micros)

    monkeypatch.setattr(ruby_rails.parser, "parse_source", recording_parse_source)
    registry.register(RubyParser())
    registry.register(ruby_rails.parser.RailsParser())

    source = b'require "rails"\nclass UsersController < ApplicationController\nend\n'
    selected = registry.select("users_controller.rb", source, parse_timeout_micros=1234)

    assert selected is not None and selected.name == "ruby-rails"
    assert parse_timeouts == [1234]


@pytest.mark.parametrize(
    ("filename", "source", "framework"),
    [
        (
            "config/routes.rb",
            b'require "rails"\nRails.application.routes.draw do\n  get "/health", to: "health#show"\nend\n',
            "rails",
        ),
        (
            "app.rb",
            b'require "sinatra"\nget "/health" do\n  "ok"\nend\n',
            "sinatra",
        ),
        (
            "api.rb",
            b'require "grape"\nclass API < Grape::API\n  get "/health" do\n    "ok"\n  end\nend\n',
            "grape",
        ),
    ],
)
def test_ruby_framework_selection_and_extraction_parse_once(
    tmp_path, monkeypatch, filename: str, source: bytes, framework: str
) -> None:
    from breezeai_cog.core import executor
    from breezeai_cog.parsers import treesitter

    registry.discover_builtin()
    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    parse_calls: list[tuple[str, int]] = []
    original_parse_source = treesitter.parse_source

    def recording_parse_source(language: str, source: bytes, timeout_micros: int = 0):
        parse_calls.append((language, timeout_micros))
        return original_parse_source(language, source, timeout_micros)

    monkeypatch.setattr(treesitter, "parse_source", recording_parse_source)
    record = executor._parse_entry(
        filename,
        str(tmp_path),
        {
            "parse_timeout_micros": 1234,
            "capture_statements": True,
            "statement_text_limit": 8000,
            "indexes": {},
        },
    )

    assert record is not None and record.framework == framework
    assert any(item.semanticType == "route" for item in record.statements)
    assert parse_calls == [("ruby", 1234)]


def test_ruby_parse_timeout_is_isolated_per_file(tmp_path, monkeypatch) -> None:
    from breezeai_cog.core import executor
    from breezeai_cog.parsers import treesitter

    registry.discover_builtin()
    path = tmp_path / "app.rb"
    path.write_bytes(b'require "sinatra"\nget "/" do\nend\n')

    def timeout_parse_source(language: str, source: bytes, timeout_micros: int = 0):
        raise ValueError("Parsing failed")

    monkeypatch.setattr(treesitter, "parse_source", timeout_parse_source)
    record = executor._parse_entry(
        "app.rb",
        str(tmp_path),
        {
            "parse_timeout_micros": 1234,
            "capture_statements": True,
            "statement_text_limit": 8000,
            "indexes": {},
        },
    )

    assert record is None


class FakeTemplateLang(BaseParser):
    """Owns both code and markup — like WebForms (.cs + .aspx) and Vue (.ts + .vue)."""

    name = "faketpl"
    extensions = (".fk", ".fkhtml")
    template_extensions = (".fkhtml",)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        return FileRecord(id=ctx.path, path=ctx.path, type="code", language="faketpl", loc=1)


def test_template_extensions_aggregates_only_declared_markup() -> None:
    registry.register(FakeLang())  # declares none — exercises the getattr default
    registry.register(FakeTemplateLang())
    assert registry.template_extensions() == {".fkhtml"}
    assert registry.capabilities()["templateExtensions"] == [".fkhtml"]


def test_template_extensions_empty_without_markup_parsers() -> None:
    registry.register(FakeLang())
    assert registry.template_extensions() == frozenset()


def test_builtin_template_extensions() -> None:
    """The real markup set the scanner gates on."""
    from breezeai_cog.core.registry import discover_builtin

    discover_builtin()
    assert registry.template_extensions() == {
        ".html", ".htm", ".cshtml", ".razor", ".aspx", ".ascx", ".master", ".vue",
    }
