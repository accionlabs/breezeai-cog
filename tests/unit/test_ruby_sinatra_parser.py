from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.ruby.parser import RubyParser
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


def test_single_sinatra_route_enriches_existing_call_without_duplicate(tmp_path: Path) -> None:
    source = b'''require "sinatra"
get('/once') { 'ok' }
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

    route_records = [item for item in record.statements if item.startLine == 2]
    assert len(route_records) == 1
    route, = route_records
    assert route.semanticType == "route"
    assert (route.method, route.endpoint, route.framework) == ("GET", "/once", "sinatra")
    assert (route.startLine, route.endLine) == (2, 2)
    assert len({item.id for item in record.statements}) == len(record.statements)


def test_sinatra_routes_support_brace_blocks_with_parenthesized_and_bare_calls(
    tmp_path: Path,
) -> None:
    source = b'''require "sinatra"
get('/parenthesized') { "ok" }
post '/bare' { "ok" }
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [
        ("GET", "/parenthesized"),
        ("POST", "/bare"),
    ]
    assert all(item.framework == "sinatra" for item in routes)
    assert [(item.startLine, item.endLine) for item in routes] == [(2, 2), (3, 3)]
    assert record.framework == "sinatra"


def test_sinatra_routes_do_not_mark_ordinary_calls_with_brace_blocks(
    tmp_path: Path,
) -> None:
    source = b'''require "sinatra"
render('/template') { "not a route" }
collect '/items' { "also not a route" }
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

    assert not any(item.semanticType == "route" for item in record.statements)


def test_sinatra_routes_support_application_receivers(tmp_path: Path) -> None:
    source = b'''require "sinatra"
@cookie_app.get('/') do
  "ok"
end
def self.registered(klass)
  klass.get('/bar') { 'bar' }
end
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [
        ("GET", "/"),
        ("GET", "/bar"),
    ]
    assert [(item.startLine, item.endLine) for item in routes] == [(2, 4), (6, 6)]
    assert all(item.framework == "sinatra" for item in routes)


def test_sinatra_routes_ignore_receiver_qualified_request_calls_with_blocks(
    tmp_path: Path,
) -> None:
    source = b'''require "sinatra"
response.get('/upstream') { |result| result }
client.post('/submit') do |result|
  result
end
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

    assert not any(item.semanticType == "route" for item in record.statements)


def test_sinatra_generic_routes_and_multiple_paths_emit_each_combination(
    tmp_path: Path,
) -> None:
    source = b'''require "sinatra"
route('PUT', 'POST', '/') { 'verb' }
route(:get, '/baz') { 'symbol' }
get('/foo', '/bar') { 'paths' }
get('/conditioned-a', '/conditioned-b', :some_condition => true) { 'condition' }
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [
        ("PUT", "/"),
        ("POST", "/"),
        ("GET", "/baz"),
        ("GET", "/foo"),
        ("GET", "/bar"),
        ("GET", "/conditioned-a"),
        ("GET", "/conditioned-b"),
    ]
    assert len({item.id for item in routes}) == len(routes)
    assert [(item.startLine, item.endLine) for item in routes] == [
        (2, 2),
        (2, 2),
        (3, 3),
        (4, 4),
        (4, 4),
        (5, 5),
        (5, 5),
    ]
    assert all(item.framework == "sinatra" and item.routeKind == "route" for item in routes)
    assert len({item.id for item in record.statements}) == len(record.statements)
    for line, expected_count in ((2, 2), (3, 1), (4, 2), (5, 2)):
        same_declaration = [item for item in routes if item.startLine == line]
        assert len(same_declaration) == expected_count
        assert len({item.id for item in same_declaration}) == expected_count
        assert {(item.startLine, item.endLine) for item in same_declaration} == {(line, line)}
    serialized = [item.model_dump(by_alias=True, exclude_none=True) for item in routes]
    assert all(item["method"] and item["endpoint"] and item["startLine"] for item in serialized)


def test_sinatra_route_detection_ignores_unrelated_and_dynamic_arguments(
    tmp_path: Path,
) -> None:
    source = b'''require "sinatra"
router.route('GET', '/') { 'not a Sinatra route' }
get('/static', DYNAMIC_PATH) { 'partial path must not be emitted' }
route(METHOD, '/dynamic') { 'unknown method' }
route('GET', DYNAMIC_PATH) { 'unknown path' }
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))

    assert not any(item.semanticType == "route" for item in record.statements)


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


@pytest.mark.parametrize(
    ("source", "endpoint"),
    [
        (
            b"class API < ::Sinatra::Base\n  get '/rooted' do; 'ok'; end\nend\n",
            "/rooted",
        ),
        (
            b"Sinatra.new do\n  get '/factory' do; 'ok'; end\nend\n",
            "/factory",
        ),
        (
            b"Class.new(Sinatra::Base) do\n  get '/anonymous' do; 'ok'; end\nend\n",
            "/anonymous",
        ),
        (
            b"require 'sinatra/base'\nget '/required' do; 'ok'; end\n",
            "/required",
        ),
    ],
    ids=["root-qualified-superclass", "sinatra-factory", "class-factory", "base-require"],
)
def test_sinatra_application_patterns_select_parser_and_extract_routes(
    tmp_path: Path, source: bytes, endpoint: str
) -> None:
    registry.clear()
    registry.register(RubyParser())
    parser = SinatraParser()
    registry.register(parser)
    try:
        selected = registry.select("app.rb", source)
        assert isinstance(selected, SinatraParser)
        record = selected.parse_file(_context(tmp_path, "app.rb", source))
    finally:
        registry.clear()

    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [("GET", endpoint)]
    assert record.framework == "sinatra"


@pytest.mark.parametrize(
    "source",
    [
        b"class PlainRuby\n  def get(path); path; end\nend\n",
        b"class PlainRuby < Base\n  def get(path); path; end\nend\n",
        b"class PlainRuby < Base\n  def configure; get('/x') { 'not a class route' }; end\nend\n",
        b"class PlainRuby\n  def run; service.get('/x') { 'response' }; end\nend\n",
        b"class PlainRuby; end\nTEXT = 'Sinatra::Base Sinatra.new'\n",
        b"# Sinatra::Base\nclass PlainRuby; end\n",
    ],
    ids=["ordinary-method", "ordinary-inherited-method", "method-local-call", "receiver-call", "string-mention", "comment-mention"],
)
def test_ordinary_ruby_patterns_do_not_select_sinatra(
    source: bytes,
) -> None:
    registry.clear()
    registry.register(RubyParser())
    registry.register(SinatraParser())
    try:
        selected = registry.select("plain.rb", source)
    finally:
        registry.clear()

    assert isinstance(selected, RubyParser)


@pytest.mark.parametrize(
    ("source", "expected_name", "expected_framework"),
    [
        (
            b"require 'grape'\nclass API < Grape::API\n  get '/x' do\n    'ok'\n  end\nend\n",
            "ruby-grape",
            "grape",
        ),
        (
            b"require 'roda'\nclass Admin < BaseApp\n  get '/x' do; 'ok'; end\nend\n",
            "ruby",
            None,
        ),
        (
            b"class Admin < BaseApp\n  get '/x' do; 'ok'; end\nend\n",
            "ruby",
            None,
        ),
    ],
    ids=["grape-derived-class", "roda-derived-class", "unidentified-derived-class"],
)
def test_bare_routes_do_not_infer_sinatra_for_other_frameworks(
    tmp_path: Path, source: bytes, expected_name: str, expected_framework: str | None
) -> None:
    registry.discover_builtin()
    try:
        selected = registry.select("app.rb", source)
        assert selected is not None and selected.name == expected_name
        record = selected.parse_file(_context(tmp_path, "app.rb", source))
    finally:
        registry.clear()

    assert record.framework == expected_framework
    assert not (
        isinstance(selected, SinatraParser)
        or any(
            item.semanticType == "route" and item.framework == "sinatra"
            for item in record.statements
        )
    )


def test_helper_loaded_sinatra_source_selects_sinatra_parser(tmp_path: Path) -> None:
    source = b'''require_relative 'spec_helper'
RSpec.describe Sinatra::Cookies do
  @cookie_app.get('/') do
    'ok'
  end
end
'''
    registry.clear()
    registry.register(RubyParser())
    registry.register(SinatraParser())
    try:
        selected = registry.select("spec/cookies_spec.rb", source)
        record = selected.parse_file(_context(tmp_path, "spec/cookies_spec.rb", source))
    finally:
        registry.clear()

    assert isinstance(selected, SinatraParser)
    # Selection is unchanged, but a spec file is a test harness: its throwaway app's
    # routes are fixtures, not application routes (parser-reference Step 5c).
    assert not any(item.semanticType == "route" for item in record.statements)
    assert record.framework is None


def test_namespace_prefixes_compose_with_route_paths(tmp_path: Path) -> None:
    source = b'''require 'sinatra'
namespace '/api' do
  namespace '/v1/' do
    get '/users/:id' do 'ok' end
    get '/' do 'root' end
    get '' do 'empty' end
    get do 'pathless' end
  end
end
get '/outside' do 'outside' end
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))
    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [
        ("GET", "/api/v1/users/:id"),
        ("GET", "/api/v1/"),
        ("GET", "/api/v1"),
        ("GET", "/api/v1"),
        ("GET", "/outside"),
    ]


def test_sinatra_interpolated_paths_and_namespaces_are_not_truncated(
    tmp_path: Path,
) -> None:
    source = b'''require 'sinatra'
namespace "/n#{i}" do
  get '/hidden' do 'unknown prefix' end
end
namespace '/api' do
  namespace '/v1' do
    get "/dynamic/#{id}" do 'unknown path' end
    get '/static' do 'ok' end
    get '/literal/#{id}' do 'single quoted literal' end
  end
end
get "/outside/#{id}" do 'unknown path' end
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))
    routes = [item for item in record.statements if item.semanticType == "route"]

    assert [(item.method, item.endpoint) for item in routes] == [
        ("GET", "/api/v1/static"),
        ("GET", "/api/v1/literal/#{id}"),
    ]
    assert not any(
        item.endpoint in {"/n/hidden", "/api/v1/dynamic/", "/outside/"}
        for item in routes
    )


def test_pathless_unprefixed_route_is_not_emitted(tmp_path: Path) -> None:
    source = b'''require 'sinatra'
get do 'no endpoint' end
'''
    record = SinatraParser().parse_file(_context(tmp_path, "app.rb", source))
    assert not any(item.semanticType == "route" for item in record.statements)


def test_require_relative_without_sinatra_evidence_stays_ruby(tmp_path: Path) -> None:
    source = b'''require_relative 'spec_helper'
class PlainRuby; end
'''
    registry.clear()
    registry.register(RubyParser())
    registry.register(SinatraParser())
    try:
        selected = registry.select("plain.rb", source)
    finally:
        registry.clear()
    assert isinstance(selected, RubyParser)


def test_similarly_named_helper_does_not_claim_plain_ruby_as_sinatra() -> None:
    source = b'''require 'my_spec_helper'
get('/') { 'not a Sinatra route' }
post('/also-not-a-route') do
  'plain Ruby'
end
'''
    registry.clear()
    registry.register(RubyParser())
    registry.register(SinatraParser())
    try:
        selected = registry.select("spec/plain_spec.rb", source)
    finally:
        registry.clear()

    assert isinstance(selected, RubyParser)


_HARNESS = b"""require 'sinatra'
get('/x') { 'ok' }
"""


def test_ruby_test_harness_files_emit_no_routes(tmp_path: Path) -> None:
    for name in (
        "spec/app_spec.rb", "test/app_test.rb", "spec/support/app.rb",
        "app_spec.rb", "lib/spec/helper_app.rb", "tests/app.rb",
    ):
        record = SinatraParser().parse_file(_context(tmp_path, name, _HARNESS))
        assert not any(i.semanticType == "route" for i in record.statements), name
        assert record.functions is not None  # still parsed for structure


def test_ruby_application_source_near_test_names_still_emits_routes(tmp_path: Path) -> None:
    for name in (
        "app.rb", "examples/simple.rb", "lib/app.rb", "app/inspector.rb",
        "contest/app.rb", "latest.rb", "spec_helper_app.rb", "specs.rb",
    ):
        record = SinatraParser().parse_file(_context(tmp_path, name, _HARNESS))
        routes = [i for i in record.statements if i.semanticType == "route"]
        assert [(r.method, r.endpoint) for r in routes] == [("GET", "/x")], name


def test_ruby_test_harness_structure_is_still_captured(tmp_path: Path) -> None:
    source = b"require 'sinatra'\nclass Helper\n  def run; end\nend\nget('/x') { }\n"
    record = SinatraParser().parse_file(_context(tmp_path, "spec/helper_spec.rb", source))
    assert [c.name for c in record.classes] == ["Helper"]
    assert [f.name for f in record.functions] == ["run"]
