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
    assert routes[0].handlerLine is None  # the action is in the controller, not on this line


def test_rails_routes_are_gated_and_dynamic_paths_are_honest_null(tmp_path: Path) -> None:
    source = b'''require "rails"\nPATH = ENV["PATH"]\nRails.application.routes.draw do\n  get PATH, to: "users#index"\nend\n'''
    without_capture = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source, False))
    with_capture = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source, True))

    assert not any(item.semanticType == "route" for item in without_capture.statements)
    route = next(item for item in with_capture.statements if item.semanticType == "route")
    assert route.endpoint is None


def test_rails_routes_ignore_non_application_route_draws(tmp_path: Path) -> None:
    source = b'''require "rails"
chart.axes.routes.draw do
  get "/not-a-route", to: "nope#nope"
end
Rails.application.routes.draw do
  get "/users", to: "users#index"
end
'''
    record = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source))

    routes = [item for item in record.statements if item.semanticType == "route"]
    assert [(item.method, item.endpoint) for item in routes] == [("GET", "/users")]


def test_rails_routes_attach_to_enclosing_class(tmp_path: Path) -> None:
        source = b'''require "rails"
class RouteConfig
    Rails.application.routes.draw do
        get "/health", to: "health#show"
    end
end
'''
        record = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source))

        route = next(item for item in record.statements if item.semanticType == "route")
        route_config = next(item for item in record.classes if item.name == "RouteConfig")
        assert route.parentId == route_config.id


def test_rails_parser_claims_only_rails_sources(tmp_path: Path) -> None:
    parser = RailsParser()
    assert parser.claims("config/routes.rb", b"Rails.application.routes.draw do\nend")
    assert not parser.claims("app.rb", b"class PlainRuby\nend")
    assert not parser.claims("lib/calc.rb", b"# Nothing to do with ApplicationController.\nclass Calculator; end")
    assert not parser.claims("lib/calc.rb", b'DOC = "ApplicationController"\nclass Calculator; end')
    assert not parser.claims("lib/chart.rb", b"chart.axes.routes.draw do\nend")
    assert parser.priority > 0
    assert parser.frameworks == ["rails"]


def test_rails_namespace_and_scope_prefixes(tmp_path: Path) -> None:
    source = b'''require "rails"
Rails.application.routes.draw do
  get "about", to: "pages#about"
  namespace :admin do
    get "stats", to: "stats#index"
    scope "/v1", module: "legacy" do
      get "/ping", to: "pages#ping"
    end
  end
  namespace :internal, path: "secret" do
    get "/ops", to: "ops#index"
  end
  scope module: "api" do
    get "/plain", to: "plain#index"
  end
  scope path: "/p" do
    get "/q", to: "q#index"
  end
  scope API_PREFIX do
    get "/dyn", to: "dyn#index"
  end
  resources :photos do
    get "preview", to: "photos#preview"
  end
end
'''
    record = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source))
    got = {r.handler: r.endpoint for r in record.statements if r.semanticType == "route"}
    assert got == {
        "pages#about": "/about",            # relative path normalised
        "stats#index": "/admin/stats",
        "pages#ping": "/admin/v1/ping",     # nested scopes compose; module: adds no path
        "ops#index": "/secret/ops",         # namespace path: overrides the name
        "plain#index": "/plain",
        "q#index": "/p/q",
        "dyn#index": None,                  # unresolved scope path → address unknown
        "photos#preview": "/photos/:photo_id/preview",  # nested in its resource
        "photos#index": "/photos",          # resources :photos expands to its actions
        "photos#create": "/photos",
        "photos#new": "/photos/new",
        "photos#edit": "/photos/:id/edit",
        "photos#show": "/photos/:id",
        "photos#update": "/photos/:id",
        "photos#destroy": "/photos/:id",
    }


def test_rails_hash_rocket_and_match_via(tmp_path: Path) -> None:
    source = b'''require "rails"
Rails.application.routes.draw do
  post "/login" => "sessions#create", as: :login
  namespace :admin do
    get "stats" => "stats#index"
  end
  get PATH => "x#y"
  match "/any", to: "x#any", via: [:get, :post]
  match "/one" => "x#one", via: :put
  match "/all", to: "x#all", via: :all
  match "/bare", to: "x#bare"
end
'''
    record = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source))
    got = sorted(
        (s.method or "", s.endpoint or "", s.handler or "")
        for s in record.statements if s.semanticType == "route"
    )
    assert got == sorted([
        ("POST", "/login", "sessions#create"),
        ("GET", "/admin/stats", "stats#index"),
        ("GET", "", "x#y"),            # non-literal path: endpoint empty, handler kept
        ("GET", "/any", "x#any"),      # one binding per verb in via:
        ("POST", "/any", "x#any"),
        ("PUT", "/one", "x#one"),
        ("ANY", "/all", "x#all"),      # via: :all → every verb (Q13)
        ("", "/bare", "x#bare"),       # no via: → method unknown
    ])
    ids = [s.id for s in record.statements if s.semanticType == "route"]
    assert len(ids) == len(set(ids))


def test_rails_before_action_guards_from_the_controller(analyze_files) -> None:
    st = analyze_files({
        "config/routes.rb": (
            "Rails.application.routes.draw do\n"
            "  get '/users/:id', to: 'users#show'\n"
            "  get '/users', to: 'users#index'\n"
            "  post '/users/:id/ban', to: 'users#ban'\n"
            "  namespace :admin do\n    get 'stats', to: 'stats#show'\n  end\n"
            "  get '/open', to: 'pages#home'\n"
            "end\n"
        ),
        "app/controllers/users_controller.rb": (
            "class UsersController < ApplicationController\n"
            "  before_action :authenticate\n"
            "  before_action :load_user, only: [:show, :ban]\n"
            "  before_action :admin_only, except: :index\n"
            "  before_action :beta, if: :beta?\n"
            "  def show; end\n  def index; end\n  def ban; end\n"
            "end\n"
        ),
        "app/controllers/admin/stats_controller.rb": (
            "module Admin\n  class StatsController < ApplicationController\n"
            "    before_action :require_admin\n    def show; end\n  end\nend\n"
        ),
        "app/controllers/stats_controller.rb": (
            "class StatsController < ApplicationController\n"
            "  before_action :public_stats\n  def show; end\nend\n"
        ),
        "app/controllers/pages_controller.rb": (
            "class PagesController < ApplicationController\n  def home; end\nend\n"
        ),
    })
    got = {s.endpoint: (s.guards, s.authRequired) for s in st if s.semanticType == "route"}
    assert got == {
        "/users/:id": (["authenticate", "load_user", "admin_only"], True),
        "/users": (["authenticate"], True),
        "/users/:id/ban": (["authenticate", "load_user", "admin_only"], True),
        # namespace :admin → Admin::StatsController, not the top-level StatsController
        # a guard, but `require_admin` is not a known authentication callback → auth unknown
        "/admin/stats": (["require_admin"], None),
        "/open": (None, None),  # no callbacks; the if: callback above is never a guard
    }


def test_rails_auth_required_only_for_authentication_callbacks(analyze_files) -> None:
    routes = "Rails.application.routes.draw do\n" + "".join(
        f"  get '/{n}', to: '{n}#index'\n"
        for n in ("devise", "login", "authz", "plain", "mixed", "scoped")
    ) + "end\n"

    def ctrl(name: str, *callbacks: str) -> tuple[str, str]:
        body = "".join(f"  {c}\n" for c in callbacks)
        return (f"app/controllers/{name}_controller.rb",
                f"class {name.capitalize()}Controller < ApplicationController\n{body}"
                "  def index; end\nend\n")

    files = dict([
        ctrl("devise", "before_action :authenticate_user!"),
        ctrl("login", "before_action :require_login"),
        ctrl("authz", "before_action :authorize"),
        ctrl("plain", "before_action :set_locale", "before_action :set_order",
             "before_action :load_resource"),
        ctrl("mixed", "before_action :set_locale", "before_action :authenticate"),
        ctrl("scoped", "before_action :authenticate_admin!, only: :other"),
    ])
    files["config/routes.rb"] = routes
    st = analyze_files(files)
    got = {s.endpoint: (s.guards, s.authRequired) for s in st if s.semanticType == "route"}
    assert got == {
        "/devise": (["authenticate_user!"], True),
        "/login": (["require_login"], True),
        "/authz": (["authorize"], True),
        # non-authentication callbacks stay in guards but never assert authRequired
        "/plain": (["set_locale", "set_order", "load_resource"], None),
        "/mixed": (["set_locale", "authenticate"], True),
        # the Devise guard is limited to another action → no guard on this route
        "/scoped": (None, None),
    }


def _guards(st: list, *endpoints: str) -> dict:
    return {s.endpoint: s.guards for s in st
            if s.semanticType == "route" and s.endpoint in endpoints}


def _routes(*lines: str) -> str:
    return "Rails.application.routes.draw do\n" + "".join(f"  {ln}\n" for ln in lines) + "end\n"


def _controller(path: str, source: str) -> tuple[str, str]:
    return f"app/controllers/{path}", source


def test_rails_inherits_callbacks_from_parent_in_another_file(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes("get '/posts', to: 'posts#index'")),
        _controller("application_controller.rb",
                    "class ApplicationController < ActionController::Base\n"
                    "  before_action :authenticate_user!\nend\n"),
        _controller("posts_controller.rb",
                    "class PostsController < ApplicationController\n"
                    "  before_action :set_post\n  def index; end\nend\n"),
    ]))
    got = [s for s in st if s.semanticType == "route"]
    assert [(s.guards, s.authRequired) for s in got] == [(["authenticate_user!", "set_post"], True)]


def test_rails_inherits_across_several_levels_in_declaration_order(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes("get '/a', to: 'leaf#index'", "get '/b', to: 'mid#index'")),
        _controller("application_controller.rb",
                    "class ApplicationController < ActionController::Base\n"
                    "  before_action :one\nend\n"),
        _controller("mid_controller.rb",
                    "class MidController < ApplicationController\n"
                    "  before_action :two\n  def index; end\nend\n"),
        _controller("leaf_controller.rb",
                    "class LeafController < MidController\n"
                    "  before_action :three\n  def index; end\nend\n"),
    ]))
    assert _guards(st, "/a", "/b") == {"/a": ["one", "two", "three"], "/b": ["one", "two"]}


def test_rails_namespaced_parent_resolves_innermost_scope_first(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes(
            "namespace :admin do", "  get 'users', to: 'users#index'",
            "  get 'plain', to: 'plain#index'", "end")),
        _controller("base_controller.rb",
                    "class BaseController < ApplicationController\n"
                    "  before_action :top_level\nend\n"),
        _controller("admin/base_controller.rb",
                    "module Admin\n  class BaseController < ApplicationController\n"
                    "    before_action :admin_scoped\n  end\nend\n"),
        # BaseController inside `module Admin` is Admin::BaseController, not ::BaseController
        _controller("admin/users_controller.rb",
                    "module Admin\n  class UsersController < BaseController\n"
                    "    def index; end\n  end\nend\n"),
        # no Admin::Nothing → falls out to the top-level one
        _controller("admin/plain_controller.rb",
                    "module Admin\n  class PlainController < ::BaseController\n"
                    "    def index; end\n  end\nend\n"),
    ]))
    assert _guards(st, "/admin/users", "/admin/plain") == {
        "/admin/users": ["admin_scoped"], "/admin/plain": ["top_level"]}


def test_rails_parent_falls_back_to_top_level_constant(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes("namespace :admin do", "  get 'users', to: 'users#index'",
                                      "end")),
        _controller("application_controller.rb",
                    "class ApplicationController < ActionController::Base\n"
                    "  before_action :authenticate\nend\n"),
        _controller("admin/users_controller.rb",
                    "module Admin\n  class UsersController < ApplicationController\n"
                    "    def index; end\n  end\nend\n"),
    ]))
    assert _guards(st, "/admin/users") == {"/admin/users": ["authenticate"]}


def test_rails_child_skips_inherited_callbacks(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes(
            "get '/s', to: 'pages#show'", "get '/i', to: 'pages#index'",
            "get '/e', to: 'pages#edit'", "get '/c', to: 'clear#index'")),
        _controller("application_controller.rb",
                    "class ApplicationController < ActionController::Base\n"
                    "  before_action :authenticate\n  before_action :audit\n"
                    "  before_action :track\nend\n"),
        _controller("pages_controller.rb",
                    "class PagesController < ApplicationController\n"
                    "  skip_before_action :authenticate, only: [:index]\n"
                    "  skip_before_action :audit, except: :show\n"
                    "  skip_before_action :track, only: :show\n"
                    "  def show; end\n  def index; end\n  def edit; end\nend\n"),
        _controller("clear_controller.rb",
                    "class ClearController < ApplicationController\n"
                    "  skip_before_action :authenticate\n  def index; end\nend\n"),
    ]))
    assert _guards(st, "/s", "/i", "/e", "/c") == {
        "/s": ["authenticate", "audit"],   # track skipped on show; audit kept (except: show)
        "/i": ["track"],  # authenticate skipped on index; audit (except: show) runs on show only
        "/e": ["authenticate", "track"],
        "/c": ["audit", "track"],
    }


def test_rails_unknown_parent_ends_inheritance(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes("get '/x', to: 'x#index'", "get '/y', to: 'y#index'")),
        # Gem::BaseController is not a controller of this repo → nothing inherited from it
        _controller("x_controller.rb",
                    "class XController < Gem::BaseController\n"
                    "  before_action :own\n  def index; end\nend\n"),
        # a parent that is not a constant path cannot be resolved either
        _controller("y_controller.rb",
                    "class YController < Struct.new(:a)\n"
                    "  before_action :own_y\n  def index; end\nend\n"),
    ]))
    assert _guards(st, "/x", "/y") == {"/x": ["own"], "/y": ["own_y"]}


def test_rails_ambiguous_parent_ends_inheritance(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes("get '/x', to: 'x#index'")),
        # BaseController is declared in two files with different callbacks → ambiguous
        _controller("base_controller.rb",
                    "class BaseController < ApplicationController\n  before_action :a\nend\n"),
        _controller("legacy/base_controller.rb",
                    "class BaseController < ApplicationController\n  before_action :b\nend\n"),
        _controller("x_controller.rb",
                    "class XController < BaseController\n"
                    "  before_action :own\n  def index; end\nend\n"),
    ]))
    assert _guards(st, "/x") == {"/x": ["own"]}


def test_rails_inheritance_cycle_terminates(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes("get '/a', to: 'a#index'")),
        _controller("a_controller.rb",
                    "class AController < BController\n  before_action :ga\n  def index; end\nend\n"),
        _controller("b_controller.rb",
                    "class BController < AController\n  before_action :gb\nend\n"),
    ]))
    assert [s.endpoint for s in st if s.semanticType == "route"] == ["/a"]


def test_rails_legacy_filter_syntax_matches_action_syntax(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes("get '/s', to: 'old#show'", "get '/i', to: 'old#index'",
                                      "get '/p', to: 'old#pre'")),
        _controller("old_controller.rb",
                    "class OldController < ApplicationController\n"
                    "  before_filter :authenticate\n"
                    "  before_filter :load, only: [:show]\n"
                    "  append_before_filter :tail\n"
                    "  prepend_before_filter :first\n"
                    "  skip_before_filter :authenticate, only: :index\n"
                    "  def show; end\n  def index; end\n  def pre; end\nend\n"),
    ]))
    got = {s.endpoint: (s.guards, s.authRequired) for s in st if s.semanticType == "route"}
    assert got == {
        "/s": (["authenticate", "load", "tail", "first"], True),
        "/i": (["tail", "first"], None),  # authenticate skipped on index
        "/p": (["authenticate", "tail", "first"], True),
    }


def test_rails_devise_authenticate_blocks_guard_their_routes(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes(
            "get '/open', to: 'pages#home'",
            "authenticate :user do",
            "  get '/account', to: 'accounts#show'",
            "  namespace :admin do",
            "    authenticate :admin do",
            "      get 'stats', to: 'stats#show'",
            "    end",
            "    get 'public', to: 'stats#public'",
            "  end",
            "end",
            "authenticated :user do",
            "  root to: 'dash#index'",
            "end",
            "unauthenticated do",
            "  get '/welcome', to: 'pages#welcome'",
            "end",
            "scope '/v1' do",
            "  get 'ping', to: 'pages#ping'",
            "end")),
        _controller("accounts_controller.rb",
                    "class AccountsController < ApplicationController\n"
                    "  before_action :authenticate_user!\n  before_action :set_locale\n"
                    "  def show; end\nend\n"),
    ]))
    got = {s.endpoint: (s.guards, s.authRequired) for s in st if s.semanticType == "route"}
    assert got == {
        "/open": (None, None),
        # the block's guard first, the controller's own after it
        "/account": (["authenticate(:user)", "authenticate_user!", "set_locale"], True),
        "/admin/stats": (["authenticate(:user)", "authenticate(:admin)"], True),
        "/admin/public": (["authenticate(:user)"], True),
        "/": (["authenticated(:user)"], True),
        "/welcome": (None, None),  # unauthenticated block: not an auth requirement
        "/v1/ping": (None, None),
    }


def test_rails_acronym_controller_matches_ignoring_case(analyze_files) -> None:
    routes = _routes("namespace :api do", "  namespace :v1 do",
                     "    get 'users', to: 'users#index'", "  end", "end")

    def api(ns: str, guard: str, path: str) -> tuple[str, str]:
        return _controller(
            path, f"module {ns}\n  module V1\n    class UsersController < ApplicationController\n"
                  f"      before_action :{guard}\n      def index; end\n    end\n  end\nend\n")

    # Api::V1 in the route, API::V1 declared (inflection acronym) → the one controller
    st = analyze_files(dict([("config/routes.rb", routes),
                             api("API", "authenticate", "api/v1/users_controller.rb")]))
    assert _guards(st, "/api/v1/users") == {"/api/v1/users": ["authenticate"]}


def test_rails_acronym_exact_match_is_preferred(analyze_files) -> None:
    routes = _routes("namespace :api do", "  namespace :v1 do",
                     "    get 'users', to: 'users#index'", "  end", "end")
    body = ("module {ns}\n  module V1\n    class UsersController < ApplicationController\n"
            "      before_action :{g}\n      def index; end\n    end\n  end\nend\n")
    # both spellings declared, but `Api::V1::UsersController` is exactly what the route names
    st = analyze_files(dict([
        ("config/routes.rb", routes),
        _controller("api/v1/users_controller.rb", body.format(ns="Api", g="exact")),
        _controller("api_acronym/v1/users_controller.rb", body.format(ns="API", g="acronym")),
    ]))
    assert _guards(st, "/api/v1/users") == {"/api/v1/users": ["exact"]}


def test_rails_acronym_ambiguous_case_variants_are_not_guessed(analyze_files) -> None:
    routes = _routes("namespace :api do", "  namespace :v1 do",
                     "    get 'users', to: 'users#index'", "  end", "end")
    body = ("module {ns}\n  module V1\n    class UsersController < ApplicationController\n"
            "      before_action :{g}\n      def index; end\n    end\n  end\nend\n")
    # neither is spelled like the route; both match ignoring case → no guess
    st = analyze_files(dict([
        ("config/routes.rb", routes),
        _controller("a/users_controller.rb", body.format(ns="API", g="one")),
        _controller("b/users_controller.rb", body.format(ns="ApI", g="two")),
    ]))
    assert _guards(st, "/api/v1/users") == {"/api/v1/users": None}


def test_rails_acronym_no_matching_controller(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes("namespace :api do", "  get 'users', to: 'users#index'",
                                      "end")),
        _controller("api/accounts_controller.rb",
                    "module API\n  class AccountsController < ApplicationController\n"
                    "    before_action :authenticate\n  end\nend\n"),
    ]))
    assert _guards(st, "/api/users") == {"/api/users": None}


def test_rails_ticket_examples_give_the_expected_output(analyze_files) -> None:
    """The examples of BREEZEAI-1426, verbatim, against the ticket's expected tables."""
    st = analyze_files({
        "config/routes.rb": (
            "Rails.application.routes.draw do\n"
            "  get '/orders/:id', to: 'orders#show'\n"
            "  get '/login', to: 'sessions#create'\n"
            "  get '/admin/users', to: 'admin/users#index'\n"
            "  get '/legacy', to: 'legacy#index'\n"
            "  authenticate :user do\n    get '/reports', to: 'reports#index'\n  end\n"
            "  namespace :api do\n    namespace :v1 do\n"
            "      get '/users/:id', to: 'users#show'\n    end\n  end\n"
            "end\n"),
        "app/controllers/application_controller.rb": (
            "class ApplicationController < ActionController::Base\n"
            "  before_action :authenticate_user!\nend\n"),
        "app/controllers/orders_controller.rb": (
            "class OrdersController < ApplicationController\n  def show; end\nend\n"),
        "app/controllers/sessions_controller.rb": (
            "class SessionsController < ApplicationController\n"
            "  skip_before_action :authenticate_user!, only: [:create]\n"
            "  def create; end\nend\n"),
        "app/controllers/admin/base_controller.rb": (
            "module Admin\n  class BaseController < ApplicationController\n"
            "    before_action :require_admin\n  end\nend\n"),
        "app/controllers/admin/users_controller.rb": (
            "module Admin\n  class UsersController < BaseController\n"
            "    def index; end\n  end\nend\n"),
        "app/controllers/legacy_controller.rb": (
            "class LegacyController < ActionController::Base\n"
            "  before_filter :require_login\n  def index; end\nend\n"),
        "app/controllers/reports_controller.rb": (
            "class ReportsController < ActionController::Base\n  def index; end\nend\n"),
        "app/controllers/api/v1/users_controller.rb": (
            "module API\n  module V1\n    class UsersController < ApplicationController\n"
            "      before_action :authenticate_token!\n      def show; end\n"
            "    end\n  end\nend\n"),
    })
    got = {s.endpoint: (s.guards, s.authRequired) for s in st if s.semanticType == "route"}
    assert got == {
        "/orders/:id": (["authenticate_user!"], True),
        "/login": (None, None),
        "/admin/users": (["authenticate_user!", "require_admin"], True),
        "/legacy": (["require_login"], True),
        "/reports": (["authenticate(:user)"], True),
        # ApplicationController's authenticate_user! is inherited here too
        "/api/v1/users/:id": (["authenticate_user!", "authenticate_token!"], True),
    }


def test_rails_orders_example_non_auth_callbacks_set_no_auth_required(analyze_files) -> None:
    st = analyze_files({
        "config/routes.rb": _routes("get '/orders/:id', to: 'orders#show'"),
        "app/controllers/orders_controller.rb": (
            "class OrdersController < ActionController::Base\n"
            "  before_action :set_locale\n  before_action :set_order, only: [:show]\n"
            "  def show; end\nend\n"),
    })
    got = [(s.guards, s.authRequired) for s in st if s.semanticType == "route"]
    assert got == [(["set_locale", "set_order"], None)]


def test_rails_redeclared_callback_replaces_the_inherited_one(analyze_files) -> None:
    st = analyze_files(dict([
        ("config/routes.rb", _routes("get '/a', to: 'child#index'", "get '/b', to: 'child#show'")),
        _controller("base_controller.rb",
                    "class BaseController < ApplicationController\n"
                    "  before_action :title\n  before_action :load\nend\n"),
        # `title` is declared again: it runs once, last, with the child's own only:
        _controller("child_controller.rb",
                    "class ChildController < BaseController\n"
                    "  before_action :title, only: [:index]\n"
                    "  def index; end\n  def show; end\nend\n"),
    ]))
    assert _guards(st, "/a", "/b") == {"/a": ["load", "title"], "/b": ["load"]}


def test_rails_login_callbacks_with_their_own_names_set_auth_required(analyze_files) -> None:
    """An app's own login callbacks count; look-alikes that only mention a user do not."""
    login = ["require_logged_in_user", "require_logged_in_moderator", "require_logged_in_admin",
             "require_logged_in_user_or_400", "authenticate_user", "require_user",
             "login_required", "ensure_logged_in"]
    other = ["load_user", "find_user_from_rss_token", "require_no_user_or_redirect",
             "check_new_users", "only_user_or_moderator", "require_logged_in_guest",
             "require_logged_in_user_or_redirect"]
    names = login + other
    files = {"config/routes.rb": _routes(*[f"get '/{n}', to: '{n}#index'" for n in names])}
    for n in names:
        files[f"app/controllers/{n}_controller.rb"] = (
            f"class {''.join(w.capitalize() for w in n.split('_'))}Controller < ApplicationController\n"
            f"  before_action :{n}\n  def index; end\nend\n")
    st = analyze_files(files)
    got = {s.endpoint.lstrip("/"): (s.guards, s.authRequired)
           for s in st if s.semanticType == "route"}
    assert got == {**{n: ([n], True) for n in login}, **{n: ([n], None) for n in other}}


def test_rails_resources_root_member_and_collection(tmp_path: Path) -> None:
    source = b'''require "rails"
Rails.application.routes.draw do
  root "home#index"
  resources :categories, only: [:index, :show] do
    resources :posts, except: :destroy
    member do
      get :preview
    end
    collection do
      get "search"
    end
    get :export, on: :collection
  end
  resource :profile, controller: "accounts"
  namespace :admin do
    root to: "dashboard#show"
    resources :people, path: "staff", param: :slug, only: :show
  end
  resources :items, only: ONLY
  resources :data
  resources :data do
    get :x
  end
end
'''
    record = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source))
    got = {(r.method, r.endpoint, r.handler)
           for r in record.statements if r.semanticType == "route"}
    def has(*row):
        assert row in got, row
    has("GET", "/", "home#index")
    has("GET", "/categories", "categories#index")
    has("GET", "/categories/:id", "categories#show")
    assert ("POST", "/categories", "categories#create") not in got   # only: honoured
    has("GET", "/categories/:category_id/posts", "posts#index")        # nested resources
    has("PATCH", "/categories/:category_id/posts/:id", "posts#update")
    has("PUT", "/categories/:category_id/posts/:id", "posts#update")
    assert not any(h == "posts#destroy" for _, _, h in got)            # except: honoured
    has("GET", "/categories/:id/preview", "categories#preview")        # member
    has("GET", "/categories/search", "categories#search")              # collection
    has("GET", "/categories/export", "categories#export")              # on: :collection
    has("GET", "/profile", "accounts#show")                            # singular, controller:
    has("GET", "/profile/edit", "accounts#edit")
    assert not any(e == "/profile/:id" for _, e, _ in got)
    has("GET", "/admin", "dashboard#show")                             # root in a namespace
    has("GET", "/admin/staff/:slug", "people#show")                    # path:, param:
    assert not any(h and h.startswith("items#") for _, _, h in got)    # only: not a literal
    has("GET", "/data/:id", "data#show")
    # "data" has no singular the rules are sure of → nested routes have no known address
    has("GET", None, "data#x")
