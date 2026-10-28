"""Role table: docs/entry-point-roles/rails.md."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point


EP = {"route", "eventbus_consumer", "timer"}
CONTROLLER = (
    "class UsersController < ApplicationController\n"
    "  before_action :authenticate\n"
    "  def show\n    render json: {}\n  end\n"
    "end\n"
)


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _routes(analyze_files, body: str, **extra: str):
    files = {"config/routes.rb": f"Rails.application.routes.draw do\n{body}end\n", **extra}
    return analyze_files(files)


def test_verb_route_is_binding(analyze_files) -> None:
    st = _routes(analyze_files, "  get '/users/:id', to: 'users#show'\n")
    (r,) = _eps(st)
    assert (r.method, r.endpoint, r.routeKind, r.framework) == (
        "GET",
        "/users/:id",
        "route",
        "rails",
    )


def test_binding_leads_to_action(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  get '/users/:id', to: 'users#show'\n",
        **{"app/controllers/users_controller.rb": CONTROLLER},
    )
    (r,) = _eps(st)
    # `users#show` is the `show` action of UsersController, in its own file (Q14).
    assert (r.handler, r.handlerPath, r.handlerLine) == (
        "show", "app/controllers/users_controller.rb", 3)


def test_relative_path_is_normalised(analyze_files) -> None:
    st = _routes(analyze_files, "  get 'about', to: 'pages#about'\n")
    assert [s.endpoint for s in _eps(st)] == ["/about"]


def test_hash_rocket_is_binding(analyze_files) -> None:
    st = _routes(analyze_files, "  post '/login' => 'sessions#create'\n")
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("POST", "/login")]


def test_match_via_is_one_binding_per_verb(analyze_files) -> None:
    st = _routes(analyze_files, "  match '/any', to: 'x#y', via: [:get, :post]\n")
    assert sorted(s.method for s in _eps(st)) == ["GET", "POST"]


def test_root_is_binding(analyze_files) -> None:
    st = _routes(analyze_files, "  root 'home#index'\n")
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("GET", "/")]


def test_resources_makes_one_binding_per_action(analyze_files) -> None:
    st = _routes(analyze_files, "  resources :users\n")
    got = {(s.method, s.endpoint) for s in _eps(st)}
    assert {
        ("GET", "/users"),
        ("GET", "/users/:id"),
        ("POST", "/users"),
        ("DELETE", "/users/:id"),
    } <= got


def test_namespace_and_scope_are_wiring(analyze_files) -> None:
    st = _routes(
        analyze_files,
        (
            "  namespace :admin do\n    get '/stats', to: 'stats#index'\n  end\n"
            "  scope '/v1' do\n    get '/ping', to: 'pages#ping'\n  end\n"
        ),
    )
    assert sorted(s.endpoint for s in _eps(st)) == ["/admin/stats", "/v1/ping"]


def test_mount_is_wiring(analyze_files) -> None:
    st = _routes(analyze_files, "  mount Sidekiq::Web, at: '/sidekiq'\n")
    assert _eps(st) == []


def test_before_action_is_guard(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  get '/users/:id', to: 'users#show'\n",
        **{"app/controllers/users_controller.rb": CONTROLLER},
    )
    assert [s.guards for s in _eps(st)] == [["authenticate"]]


def test_unrouted_action_is_none(analyze_files) -> None:
    st = analyze_files({"app/controllers/users_controller.rb": CONTROLLER})
    assert _eps(st) == []


def _guards_by_action(analyze_files, controller_body: str) -> dict[str, tuple]:
    routes = "".join(f"  get '/{a}', to: 'sessions#{a}'\n" for a in ("new", "create", "destroy"))
    controller = ("class SessionsController < ApplicationController\n" + controller_body
                  + "".join(f"  def {a}; end\n" for a in ("new", "create", "destroy")) + "end\n")
    st = _routes(analyze_files, routes,
                 **{"app/controllers/sessions_controller.rb": controller})
    return {s.endpoint.lstrip("/"): (s.guards, s.authRequired) for s in _eps(st)}


GUARDED = (["authenticate_user!"], True)
OPEN = (None, None)


def test_skip_only_removes_the_guard_from_those_actions(analyze_files) -> None:
    got = _guards_by_action(analyze_files, "  before_action :authenticate_user!\n"
                            "  skip_before_action :authenticate_user!, only: [:new, :create]\n")
    assert got == {"new": OPEN, "create": OPEN, "destroy": GUARDED}


def test_skip_except_keeps_the_guard_only_on_those_actions(analyze_files) -> None:
    got = _guards_by_action(analyze_files, "  before_action :authenticate_user!\n"
                            "  skip_before_action :authenticate_user!, except: :destroy\n")
    assert got == {"new": OPEN, "create": OPEN, "destroy": GUARDED}


def test_skip_composes_with_the_callbacks_own_action_list(analyze_files) -> None:
    got = _guards_by_action(analyze_files,
                            "  before_action :authenticate_user!, except: [:new]\n"
                            "  skip_before_action :authenticate_user!, only: :create\n")
    assert got == {"new": OPEN, "create": OPEN, "destroy": GUARDED}


def test_conditional_or_unreadable_skip_removes_the_guard_everywhere(analyze_files) -> None:
    for skip in ("  skip_before_action :authenticate_user!, if: :api?\n",
                 "  skip_before_action :authenticate_user!, only: ACTIONS\n",
                 "  skip_before_action callback_name\n"):
        got = _guards_by_action(analyze_files, "  before_action :authenticate_user!\n" + skip)
        assert got == {"new": OPEN, "create": OPEN, "destroy": OPEN}, skip


def test_skip_applies_to_a_parents_callback(analyze_files) -> None:
    # The parent in the same file: its callback is inherited, then skipped for create.
    controller = (
        "class ApplicationController < ActionController::Base\n"
        "  before_action :authenticate_user!\nend\n"
        "class SessionsController < ApplicationController\n"
        "  skip_before_action :authenticate_user!, only: :create\n"
        "  def create; end\n  def destroy; end\nend\n")
    st = _routes(analyze_files, "  post '/login', to: 'sessions#create'\n"
                 "  delete '/logout', to: 'sessions#destroy'\n",
                 **{"app/controllers/sessions_controller.rb": controller})
    assert {s.endpoint: (s.guards, s.authRequired) for s in _eps(st)} == {
        "/login": OPEN, "/logout": GUARDED}
