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
        "/admin/stats": (["require_admin"], True),
        "/open": (None, None),  # no callbacks; the if: callback above is never a guard
    }


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

def test_rails_routes_skip_spec_files_but_not_config_routes(tmp_path: Path) -> None:
    source = b'Rails.application.routes.draw do\n  get "/x", to: "a#b"\nend\n'
    spec = RailsParser().parse_file(_context(tmp_path, "spec/routing_spec.rb", source))
    cfg = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source))
    assert not any(s.semanticType == "route" for s in spec.statements)
    assert [s.endpoint for s in cfg.statements if s.semanticType == "route"] == ["/x"]


def _rails_routes(tmp_path: Path, body: str):
    source = ('require "rails"\nRails.application.routes.draw do\n' + body + "end\n").encode()
    record = RailsParser().parse_file(_context(tmp_path, "config/routes.rb", source))
    return [(s.method, s.endpoint, s.handler) for s in record.statements if s.semanticType == "route"]


def test_rails_namespace_and_scope_prefix_routes(tmp_path: Path) -> None:
    body = '''  get "/top", to: "a#b"
  namespace :admin do
    get "dashboard", to: "admin#dash"
    post "/users", to: "users#create"
  end
  scope "/api" do
    get "scoped", to: "s#t"
  end
  scope "v1" do
    get "/x", to: "x#y"
  end
  get "after", to: "a#c"
'''
    assert _rails_routes(tmp_path, body) == [
        ("GET", "/top", "a#b"),
        ("GET", "/admin/dashboard", "admin#dash"),
        ("POST", "/admin/users", "users#create"),
        ("GET", "/api/scoped", "s#t"),
        ("GET", "/v1/x", "x#y"),
        ("GET", "/after", "a#c"),
    ]


def test_rails_nested_namespace_and_scope_compose(tmp_path: Path) -> None:
    body = '''  namespace :admin do
    namespace :v2 do
      get "deep", to: "d#e"
    end
    scope "/reports" do
      namespace "monthly" do
        get "/", to: "r#m"
      end
    end
    get "sibling", to: "s#t"
  end
  scope module: "web" do
    get "modonly", to: "w#x"
  end
  scope path: "/p" do
    get "viapath", to: "p#q"
  end
  namespace :admin, path: "control" do
    get "x", to: "c#x"
  end
  constraints(subdomain: "api") do
    namespace :t do
      get "wrapped", to: "t#w"
    end
  end
'''
    assert _rails_routes(tmp_path, body) == [
        ("GET", "/admin/v2/deep", "d#e"),
        ("GET", "/admin/reports/monthly", "r#m"),
        ("GET", "/admin/sibling", "s#t"),
        ("GET", "/modonly", "w#x"),
        ("GET", "/p/viapath", "p#q"),
        ("GET", "/control/x", "c#x"),
        ("GET", "/t/wrapped", "t#w"),
    ]


def test_rails_dynamic_or_unsupported_prefix_leaves_endpoint_null(tmp_path: Path) -> None:
    body = '''  namespace "ns#{i}" do
    get "a", to: "a#a"
  end
  namespace prefix_var do
    get "b", to: "b#b"
  end
  scope PREFIX do
    get "c", to: "c#c"
  end
  scope :sym do
    get "d", to: "d#d"
  end
  scope path: dynamic do
    get "e", to: "e#e"
  end
  resources :users do
    get "f", to: "f#f"
  end
  namespace :admin do
    resources :things do
      member { get "g", to: "g#g" }
    end
    get "ok", to: "o#k"
  end
'''
    key = lambda route: (route[0] or "", route[1] or "", route[2] or "")
    assert sorted(_rails_routes(tmp_path, body), key=key) == sorted([
        ("GET", None, "a#a"),
        ("GET", None, "b#b"),
        ("GET", None, "c#c"),
        ("GET", None, "d#d"),
        ("GET", None, "e#e"),
        ("GET", "/users", "users#index"),
        ("POST", "/users", "users#create"),
        ("GET", "/users/new", "users#new"),
        ("GET", "/users/:id/edit", "users#edit"),
        ("GET", "/users/:id", "users#show"),
        ("PATCH", "/users/:id", "users#update"),
        ("PUT", "/users/:id", "users#update"),
        ("DELETE", "/users/:id", "users#destroy"),
        ("GET", "/users/:user_id/f", "f#f"),
        ("GET", "/admin/things", "things#index"),
        ("POST", "/admin/things", "things#create"),
        ("GET", "/admin/things/new", "things#new"),
        ("GET", "/admin/things/:id/edit", "things#edit"),
        ("GET", "/admin/things/:id", "things#show"),
        ("PATCH", "/admin/things/:id", "things#update"),
        ("PUT", "/admin/things/:id", "things#update"),
        ("DELETE", "/admin/things/:id", "things#destroy"),
        ("GET", "/admin/things/:id/g", "g#g"),
        ("GET", "/admin/ok", "o#k"),
    ], key=key)


def test_rails_interpolated_paths_are_null_and_static_routes_remain_correct(
    tmp_path: Path,
) -> None:
    body = '''  get "/outside/#{id}", to: "items#show"
  namespace :admin do
    get "/dynamic/#{id}", to: "admin#show"
    get "/static", to: "admin#index"
    get '/literal/#{id}', to: 'admin#literal'
  end
'''

    assert _rails_routes(tmp_path, body) == [
        ("GET", None, "items#show"),
        ("GET", None, "admin#show"),
        ("GET", "/admin/static", "admin#index"),
        ("GET", "/admin/literal/#{id}", "admin#literal"),
    ]
