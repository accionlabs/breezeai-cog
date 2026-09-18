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
    api = next(item for item in record.classes if item.name == "API")
    assert next(item for item in routes if item.endpoint == "/users").parentId == api.id
    assert {item.name for item in record.classes} >= {"API"}


def test_sinatra_routes_require_literal_paths_and_capture(tmp_path: Path) -> None:
    source = b'''require "sinatra"\nPATH = ENV["PATH"]\nget PATH do\n  "ok"\nend\n'''
    without_capture = SinatraParser().parse_file(_context(tmp_path, "app.rb", source, False))
    with_capture = SinatraParser().parse_file(_context(tmp_path, "app.rb", source, True))

    assert not any(item.semanticType == "route" for item in without_capture.statements)
    assert not any(item.semanticType == "route" for item in with_capture.statements)


def test_sinatra_routes_ignore_receiver_qualified_http_calls(tmp_path: Path) -> None:
    source = b'''require "sinatra"
get "/users" do
  "ok"
end
cache.delete "/cache" do
  "ignored"
end
http_pool.post "/upstream" do
  "ignored"
end
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [("GET", "/users")]


def test_sinatra_routes_prefer_enclosing_function_owner(tmp_path: Path) -> None:
        source = b'''require "sinatra"
class API < Sinatra::Base
    def register
        get "/users" do
            "ok"
        end
    end
end
'''
        record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

        route = next(item for item in record.statements if item.semanticType == "route")
        register = next(item for item in record.functions if item.name == "register")
        assert route.parentId == register.id


def test_sinatra_parser_claims_only_sinatra_sources(tmp_path: Path) -> None:
    parser = SinatraParser()
    assert parser.claims("app.rb", b"require 'sinatra'\nget '/' do\nend")
    assert parser.claims("app.rb", b"class API < Sinatra::Base\nend")
    assert not parser.claims("app.rb", b"class PlainRuby\nend")
    assert not parser.claims("app.rb", b'DOC = "see Sinatra::Base for details"')
    assert not parser.claims("app.rb", b"# Sinatra::Application is mentioned here\nclass PlainRuby; end")
    assert parser.frameworks == ["sinatra"]
