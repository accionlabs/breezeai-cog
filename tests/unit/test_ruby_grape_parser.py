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
    api = next(item for item in record.classes if item.name == "API")
    assert all(item.parentId == api.id for item in routes)
    assert {item.name for item in record.classes} >= {"API"}


def test_grape_dynamic_prefix_is_not_guessed(tmp_path: Path) -> None:
    source = b'''require "grape"\nclass API < Grape::API\n  namespace API_NAMESPACE do\n    get "/users" do\n    end\n  end\nend\n'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))

    # The route is kept; its prefix exists but cannot be resolved, so the address is unknown.
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(r.method, r.endpoint) for r in routes] == [("GET", None)]


def test_grape_routes_ignore_receiver_qualified_http_calls(tmp_path: Path) -> None:
        source = b'''require "grape"
class API < Grape::API
    helpers do
        def cached
            store.get "users:all" do
                "ignored"
            end
        end
    end
    get "/users" do
    end
end
'''
        record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))

        routes = [item for item in record.statements if item.semanticType == "route"]
        assert [(item.method, item.endpoint) for item in routes] == [("GET", "/users")]


def test_grape_routes_are_gated_and_claims_are_specific(tmp_path: Path) -> None:
    source = b'''require "grape"\nclass API < Grape::API\n  get "/health" do\n  end\nend\n'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source, False))
    parser = GrapeParser()

    assert not any(item.semanticType == "route" for item in record.statements)
    assert parser.claims("api.rb", source)
    assert not parser.claims("plain.rb", b"class API\nend")
    assert not parser.claims("plain.rb", b'DOC = "Grape::API is documented here"')
    assert not parser.claims("plain.rb", b"# Grape::Endpoint is mentioned here\nclass PlainRuby; end")
    assert parser.frameworks == ["grape"]


def test_grape_block_handler_has_line_only(tmp_path: Path) -> None:
    # The handler is the route's anonymous block: no name, handlerLine = where the block starts.
    source = b'''require "grape"\n\nclass API < Grape::API\n  get "/a" do\n    1\n  end\n  namespace :n do\n    post "/b" do\n      2\n    end\n  end\nend\n'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    got = {s.endpoint: (s.handler, s.handlerLine) for s in record.statements if s.semanticType == "route"}
    assert got == {"/a": (None, 4), "/n/b": (None, 8)}


def test_grape_prefix_and_version(tmp_path: Path) -> None:
    source = b'''require "grape"
class API < Grape::API
  prefix :api
  get "/before" do
  end
  version "v1"
  namespace :users do
    get "/list" do
    end
  end
  version "v2", using: :header, vendor: "acme"
  get "/hdr" do
  end
end
class Other < Grape::API
  prefix API_PREFIX
  get "/x" do
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    got = {r.startLine: r.endpoint for r in record.statements if r.semanticType == "route"}
    assert got == {
        4: "/api/before",          # prefix applies; no version yet
        8: "/api/v1/users/list",   # prefix / version (path strategy) / namespace / path
        12: "/api/hdr",            # header versioning adds no path
        17: None,                  # unresolved prefix → address unknown
    }
