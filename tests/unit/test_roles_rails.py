"""Role table: docs/entry-point-roles/rails.md."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}
CONTROLLER = (
    "class UsersController < ApplicationController\n"
    "  before_action :authenticate\n"
    "  def show\n    render json: {}\n  end\n"
    "end\n"
)


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


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


@pytest.mark.xfail(
    strict=True,
    reason="handler is 'users#show' with no handlerLine: the action's file is not linked yet",
)
def test_binding_leads_to_action(analyze_files) -> None:
    st = _routes(
        analyze_files,
        "  get '/users/:id', to: 'users#show'\n",
        **{"app/controllers/users_controller.rb": CONTROLLER},
    )
    (r,) = _eps(st)
    assert (r.handler, r.handlerLine) == ("show", 3)


def test_relative_path_is_normalised(analyze_files) -> None:
    st = _routes(analyze_files, "  get 'about', to: 'pages#about'\n")
    assert [s.endpoint for s in _eps(st)] == ["/about"]


@pytest.mark.xfail(strict=True, reason="hash-rocket form leaves endpoint and handler empty")
def test_hash_rocket_is_binding(analyze_files) -> None:
    st = _routes(analyze_files, "  post '/login' => 'sessions#create'\n")
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("POST", "/login")]


@pytest.mark.xfail(strict=True, reason="match ... via: gives one record with no method")
def test_match_via_is_one_binding_per_verb(analyze_files) -> None:
    st = _routes(analyze_files, "  match '/any', to: 'x#y', via: [:get, :post]\n")
    assert sorted(s.method for s in _eps(st)) == ["GET", "POST"]


@pytest.mark.xfail(strict=True, reason="root route not detected")
def test_root_is_binding(analyze_files) -> None:
    st = _routes(analyze_files, "  root 'home#index'\n")
    assert [(s.method, s.endpoint) for s in _eps(st)] == [("GET", "/")]


@pytest.mark.xfail(strict=True, reason="resources / resource not expanded into routes")
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


@pytest.mark.xfail(strict=True, reason="before_action not recorded in guards")
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
