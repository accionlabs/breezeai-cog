from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.ruby_sinatra.parser import SinatraParser


def _context(tmp_path: Path, name: str, source: bytes, capture: bool = True) -> ParseContext:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(source)
    return ParseContext(
        path=name,
        abs_path=path,
        source=source,
        repo_root=tmp_path,
        capture_statements=capture,
    )


def test_sinatra_routes_and_base_extraction(tmp_path: Path) -> None:
    source = b'''require "sinatra"\n\nget "/users/:id" do\n  "ok"\nend\n\nclass API < Sinatra::Base\n  post "/users" do\n    create_user\n  end\nend\n'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

    assert record.framework == "sinatra"
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert len(routes) == 2
    assert {(item.method, item.endpoint) for item in routes} == {
        ("GET", "/users/:id"),
        ("POST", "/users"),
    }
    assert {item.name for item in record.classes} >= {"API"}


def test_sinatra_routes_require_literal_paths_and_capture(tmp_path: Path) -> None:
    source = b'''require "sinatra"\nPATH = ENV["PATH"]\nget PATH do\n  "ok"\nend\n'''
    without_capture = SinatraParser().parse_file(_context(tmp_path, "app.rb", source, False))
    with_capture = SinatraParser().parse_file(_context(tmp_path, "app.rb", source, True))

    assert not any(item.semanticType == "route" for item in without_capture.statements)
    assert not any(item.semanticType == "route" for item in with_capture.statements)


def test_sinatra_parser_claims_only_sinatra_sources(tmp_path: Path) -> None:
    parser = SinatraParser()
    assert parser.claims("app.rb", b"require 'sinatra'\nget '/' do\nend")
    assert parser.claims("app.rb", b"class API < Sinatra::Base\nend")
    assert not parser.claims("app.rb", b"class PlainRuby\nend")
    assert parser.frameworks == ["sinatra"]
