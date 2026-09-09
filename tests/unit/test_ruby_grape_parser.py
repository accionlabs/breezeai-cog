from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.ruby_grape.parser import GrapeParser


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


def test_grape_routes_and_nested_prefixes(tmp_path: Path) -> None:
    source = b'''require "grape"\n\nclass API < Grape::API\n  namespace :users do\n    route_param :id do\n      get do\n        "ok"\n      end\n    end\n    post "/bulk" do\n    end\n  end\n  get "/health" do\n  end\nend\n'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))

    assert record.framework == "grape"
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert {(item.method, item.endpoint) for item in routes} == {
        ("GET", "/users/:id"),
        ("POST", "/users/bulk"),
        ("GET", "/health"),
    }
    assert {item.name for item in record.classes} >= {"API"}


def test_grape_dynamic_prefix_is_not_guessed(tmp_path: Path) -> None:
    source = b'''require "grape"\nclass API < Grape::API\n  namespace API_NAMESPACE do\n    get "/users" do\n    end\n  end\nend\n'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))

    assert not any(item.semanticType == "route" for item in record.statements)


def test_grape_routes_are_gated_and_claims_are_specific(tmp_path: Path) -> None:
    source = b'''require "grape"\nclass API < Grape::API\n  get "/health" do\n  end\nend\n'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source, False))
    parser = GrapeParser()

    assert not any(item.semanticType == "route" for item in record.statements)
    assert parser.claims("api.rb", source)
    assert not parser.claims("plain.rb", b"class API\nend")
    assert parser.frameworks == ["grape"]
