from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.ruby_rails.parser import RailsParser


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


def test_rails_routes_and_base_extraction(tmp_path: Path) -> None:
    source = b'''require "rails"\n\nclass UsersController < ApplicationController\n  def index\n    render json: User.all\n  end\nend\n\nRails.application.routes.draw do\n  get "/users", to: "users#index"\nend\n'''
    record = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source))

    assert record.framework == "rails"
    assert {item.name for item in record.classes} >= {"UsersController"}
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].method == "GET"
    assert routes[0].endpoint == "/users"
    assert routes[0].handler == "users#index"


def test_rails_routes_are_gated_and_dynamic_paths_are_honest_null(tmp_path: Path) -> None:
    source = b'''require "rails"\nPATH = ENV["PATH"]\nRails.application.routes.draw do\n  get PATH, to: "users#index"\nend\n'''
    without_capture = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source, False))
    with_capture = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source, True))

    assert not any(item.semanticType == "route" for item in without_capture.statements)
    route = next(item for item in with_capture.statements if item.semanticType == "route")
    assert route.endpoint is None


def test_rails_parser_claims_only_rails_sources(tmp_path: Path) -> None:
    parser = RailsParser()
    assert parser.claims("config/routes.rb", b"Rails.application.routes.draw do\nend")
    assert not parser.claims("app.rb", b"class PlainRuby\nend")
    assert parser.priority > 0
    assert parser.frameworks == ["rails"]
