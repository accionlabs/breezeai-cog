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


def test_grape_static_prefix_and_path_version(tmp_path: Path) -> None:
    source = b'''require "grape"
class API < Grape::API
  prefix :api
  version 'v1', using: :path
  get '/' do
    'hello'
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    route = next(item for item in record.statements if item.semanticType == "route")

    assert (route.method, route.endpoint, route.version) == ("GET", "/api/v1/", "v1")
    assert route.framework == "grape"
    assert route.startLine == 5
    api = next(item for item in record.classes if item.name == "API")
    assert route.parentId == api.id


def test_grape_path_version_composes_with_namespace_and_route_param(tmp_path: Path) -> None:
    source = b'''require "grape"
class API < Grape::API
  prefix :api
  version 'v1', using: :path
  namespace :users do
    route_param :id do
      get '/' do
        'ok'
      end
    end
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    route = next(item for item in record.statements if item.semanticType == "route")

    assert (route.endpoint, route.version) == ("/api/v1/users/:id", "v1")


def test_grape_non_path_version_is_metadata_only(tmp_path: Path) -> None:
    source = b'''require "grape"
class API < Grape::API
  prefix :api
  version 'v1', using: :header
  get '/users' do
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    route = next(item for item in record.statements if item.semanticType == "route")

    assert route.endpoint == "/api/users"
    assert route.version == "v1"


def test_grape_dynamic_version_is_not_guessed(tmp_path: Path) -> None:
    source = b'''require "grape"
class API < Grape::API
  prefix :api
  version API_VERSION, using: :path
  get '/users' do
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))

    assert not any(item.semanticType == "route" for item in record.statements)


def test_grape_prefix_and_version_do_not_leak_between_api_classes(tmp_path: Path) -> None:
    source = b'''require "grape"
class VersionedAPI < Grape::API
  prefix :api
  version 'v1', using: :path
  get '/users' do
  end
end
class PlainAPI < Grape::API
  get '/health' do
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    routes = {item.endpoint: item for item in record.statements if item.semanticType == "route"}

    assert routes["/api/v1/users"].version == "v1"
    assert routes["/health"].version is None


def test_grape_dynamic_prefix_is_not_guessed(tmp_path: Path) -> None:
    source = b'''require "grape"\nclass API < Grape::API\n  namespace API_NAMESPACE do\n    get "/users" do\n    end\n  end\nend\n'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))

    assert not any(item.semanticType == "route" for item in record.statements)


def test_grape_routes_require_an_exact_grape_superclass(tmp_path: Path) -> None:
    source = b'''require "grape"\nclass API < NotGrape::API\n  get "/wrong" do\n    "ignored"\n  end\nend\n'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))

    assert not any(item.semanticType == "route" for item in record.statements)


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


def test_grape_routes_skip_spec_files_but_not_application_source(tmp_path: Path) -> None:
    source = b"require 'grape'\nclass API < Grape::API\n  get '/x' do\n  end\nend\n"
    spec = GrapeParser().parse_file(_context(tmp_path, "spec/api_spec.rb", source))
    app = GrapeParser().parse_file(_context(tmp_path, "benchmark/api.rb", source))
    assert not any(s.semanticType == "route" for s in spec.statements)
    assert [s.endpoint for s in app.statements if s.semanticType == "route"] == ["/x"]


def _grape_routes(tmp_path: Path, body: str):
    source = ("require 'grape'\nclass API < Grape::API\n" + body + "end\n").encode()
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    return [(s.method, s.endpoint) for s in record.statements if s.semanticType == "route"]


def test_grape_interpolated_route_path_is_not_emitted_as_a_partial_literal(
    tmp_path: Path,
) -> None:
    body = '''  get "/resource#{index}/:id" do
  end
  get "/resource#{index}" do
  end
  get "#{base}/x" do
  end
  get "/a/#{x}/b/#{y}" do
  end
  get "#{only}" do
  end
  post :"sym#{x}" do
  end
'''
    assert _grape_routes(tmp_path, body) == []


def test_grape_interpolation_in_enclosing_prefix_or_namespace_drops_routes(
    tmp_path: Path,
) -> None:
    body = '''  prefix "api#{v}"
  get '/a' do
  end
'''
    assert _grape_routes(tmp_path, body) == []
    body = '''  namespace "ns#{i}" do
    get '/a' do
    end
  end
  route_param "p#{i}" do
    get '/b' do
    end
  end
  get '/static' do
  end
'''
    assert _grape_routes(tmp_path, body) == [("GET", "/static")]


def test_grape_interpolated_path_version_is_not_guessed(tmp_path: Path) -> None:
    body = '''  version "v#{n}", using: :path
  get '/a' do
  end
'''
    assert _grape_routes(tmp_path, body) == []


def test_grape_static_strings_and_parameters_still_emit(tmp_path: Path) -> None:
    body = '''  get "/resource/:id" do
  end
  get '/single/#{literal}' do
  end
  namespace "static" do
    get "" do
    end
    route_param :id do
      get "sub/:name" do
      end
    end
  end
'''
    assert _grape_routes(tmp_path, body) == [
        ("GET", "/resource/:id"),
        ("GET", "/single/#{literal}"),
        ("GET", "/static"),
        ("GET", "/static/:id/sub/:name"),
    ]
