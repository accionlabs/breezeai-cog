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

    routes = [item for item in record.statements if item.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].endpoint is None


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

    # The route is kept; its prefix exists but cannot be resolved, so the address is unknown.
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(r.method, r.endpoint) for r in routes] == [("GET", None)]


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


def test_grape_before_hooks_are_guards(tmp_path: Path) -> None:
    source = b'''require "grape"
class Api < Grape::API
  before { authenticate! }
  get '/early' do
    1
  end
  before do
    check_scope(:read)
  end
  namespace :admin do
    before { require_admin! }
    before do
      log; audit
    end
    get '/stats' do
      2
    end
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    got = {s.endpoint: (s.guards, s.authRequired) for s in record.statements if s.semanticType == "route"}
    # A before hook is a scope-level guard: listed, but it never asserts auth (it may set a
    # header or a locale) — authRequired stays unknown.
    assert got == {
        "/early": (["authenticate!"], None),
        # outer scope first; a hook with more than one statement names no guard
        "/admin/stats": (["authenticate!", "check_scope", "require_admin!"], None),
    }


def test_grape_scope_summaries_keep_source_order_and_nested_inheritance(
    tmp_path: Path, monkeypatch
) -> None:
    from breezeai_cog.parsers.ruby_grape import routes as route_module

    source = b'''require "grape"
class API < Grape::API
  prefix :api
  version "v1", using: :path
  before { authenticate! }
  get "/early" do
  end
  version "v2", using: :header
  before { audit! }
  namespace :admin do
    before { admin! }
    get "/users" do
    end
  end
  get "/late" do
  end
end
'''
    summarized = []
    original = route_module._ScopeSummaries.body

    def count_body(self, body):
        key = (body.start_byte, body.end_byte)
        if key not in self._bodies:
            summarized.append(key)
        return original(self, body)

    monkeypatch.setattr(route_module._ScopeSummaries, "body", count_body)
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    routes = {r.endpoint: (r.version, r.guards) for r in record.statements if r.semanticType == "route"}

    assert routes == {
        "/api/v1/early": ("v1", ["authenticate!"]),
        "/api/admin/users": ("v2", ["authenticate!", "audit!", "admin!"]),
        "/api/late": ("v2", ["authenticate!", "audit!"]),
    }
    assert len(summarized) == len(set(summarized))
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
    assert _grape_routes(tmp_path, body) == [
        ("GET", None), ("GET", None), ("GET", None),
        ("GET", None), ("GET", None), ("POST", None),
    ]


def test_grape_interpolation_in_route_paths_is_dropped_but_unknown_prefixes_stay_null(
    tmp_path: Path,
) -> None:
    body = '''  prefix "api#{v}"
  get '/a' do
  end
'''
    assert _grape_routes(tmp_path, body) == [("GET", None)]
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
    assert _grape_routes(tmp_path, body) == [
        ("GET", None),
        ("GET", None),
        ("GET", "/static"),
    ]


def test_grape_interpolated_path_version_is_not_guessed(tmp_path: Path) -> None:
    body = '''  version "v#{n}", using: :path
  get '/a' do
  end
'''
    assert _grape_routes(tmp_path, body) == [("GET", None)]


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


def test_grape_multiple_paths_verbs_and_class_new_api(tmp_path: Path) -> None:
    source = b'''require "grape"
API = Class.new(Grape::API) do
  route [:get, :post], ["/one", "/two"] do
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [
        ("GET", "/one"), ("GET", "/two"), ("POST", "/one"), ("POST", "/two"),
    ]


def test_grape_mounted_sub_api_prefix_and_unknown_mount_are_honest(tmp_path: Path) -> None:
    source = b'''require "grape"
class MainAPI < Grape::API
  mount ChildAPI => "/child"
end
class ChildAPI < Grape::API
  get "/users" do
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [("GET", "/child/users")]

    unknown = source.replace(b'ChildAPI => "/child"', b"ChildAPI")
    record = GrapeParser().parse_file(_context(tmp_path, "unknown.rb", unknown))
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [("GET", None)]


def test_grape_static_version_arrays_expand_each_version(tmp_path: Path) -> None:
    source = b'''require "grape"
class API < Grape::API
  prefix :api
  version %i[v1 v2], using: :path
  get "/users" do
  end
end
'''
    record = GrapeParser().parse_file(_context(tmp_path, "api.rb", source))
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.version, item.endpoint) for item in routes] == [
        ("v1", "/api/v1/users"),
        ("v2", "/api/v2/users"),
    ]
