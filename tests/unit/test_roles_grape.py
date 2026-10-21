"""Role table: docs/entry-point-roles/grape.md."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

EP = {"route", "eventbus_consumer", "timer"}


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _api(analyze_files, body: str):
    return analyze_files({"api.rb": f"require 'grape'\nclass API < Grape::API\n{body}end\n"})


def test_verb_block_is_binding(analyze_files) -> None:
    (r,) = _eps(_api(analyze_files, "  get '/status' do\n    1\n  end\n"))
    assert (r.method, r.endpoint, r.routeKind, r.framework, r.handler) == (
        "GET",
        "/status",
        "route",
        "grape",
        None,
    )


def test_anonymous_handler_has_line(analyze_files) -> None:
    (r,) = _eps(_api(analyze_files, "  get '/status' do\n    1\n  end\n"))
    assert r.handlerLine == 3


def test_namespace_and_route_param_are_wiring(analyze_files) -> None:
    st = _api(
        analyze_files,
        (
            "  namespace :users do\n    get ':id' do\n      1\n    end\n"
            "    route_param :id do\n      post :items do\n        2\n      end\n    end\n  end\n"
        ),
    )
    assert sorted((s.method, s.endpoint) for s in _eps(st)) == [
        ("GET", "/users/:id"),
        ("POST", "/users/:id/items"),
    ]


def test_prefix_and_version_are_wiring(analyze_files) -> None:
    st = _api(analyze_files, "  prefix 'api'\n  version 'v1'\n  get '/status' do\n    1\n  end\n")
    assert [s.endpoint for s in _eps(st)] == ["/api/v1/status"]


def test_mount_is_wiring(analyze_files) -> None:
    st = _api(analyze_files, "  mount Other::API\n")
    assert _eps(st) == []


def test_before_is_guard(analyze_files) -> None:
    st = _api(analyze_files, "  before { authenticate! }\n  get '/status' do\n    1\n  end\n")
    assert len(_eps(st)) == 1
    assert _eps(st)[0].guards


def test_desc_and_params_are_none(analyze_files) -> None:
    st = _api(
        analyze_files,
        (
            "  desc 'Status'\n  params do\n    requires :id, type: Integer\n  end\n"
            "  get '/status' do\n    1\n  end\n"
        ),
    )
    assert [s.endpoint for s in _eps(st)] == ["/status"]
