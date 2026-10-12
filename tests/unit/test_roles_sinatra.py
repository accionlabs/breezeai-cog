"""Role table: docs/entry-point-roles/sinatra.md."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


APP = "require 'sinatra'\n\nbefore do\n  auth!\nend\n\nget '/hi' do\n  'hi'\nend\n"


def test_verb_block_is_binding(analyze_files) -> None:
    (r,) = _eps(analyze_files({"app.rb": APP}))
    assert (r.method, r.endpoint, r.routeKind, r.framework, r.handler) == (
        "GET",
        "/hi",
        "route",
        "sinatra",
        None,
    )


def test_anonymous_handler_has_line(analyze_files) -> None:
    (r,) = _eps(analyze_files({"app.rb": APP}))
    assert r.handlerLine == 7


def test_namespace_is_wiring(analyze_files) -> None:
    st = analyze_files(
        {
            "app.rb": (
                "require 'sinatra'\nrequire 'sinatra/namespace'\n"
                "namespace '/api' do\n  get '/x' do\n    1\n  end\nend\n"
            )
        }
    )
    assert [s.endpoint for s in _eps(st)] == ["/api/x"]


@pytest.mark.xfail(strict=True, reason="before filter not recorded in guards")
def test_before_filter_is_guard(analyze_files) -> None:
    st = analyze_files({"app.rb": APP})
    assert len(_eps(st)) == 1
    assert _eps(st)[0].guards


@pytest.mark.xfail(strict=True, reason="Rack middleware (use ...) not recorded in guards")
def test_use_middleware_is_guard(analyze_files) -> None:
    st = analyze_files(
        {"app.rb": ("require 'sinatra'\nuse Rack::Auth::Basic\n\nget '/hi' do\n  'hi'\nend\n")}
    )
    assert [s.guards for s in _eps(st)] == [["Rack::Auth::Basic"]]


def test_startup_is_none(analyze_files) -> None:
    st = analyze_files({"app.rb": "require 'sinatra'\nset :port, 4567\nrun!\n"})
    assert _eps(st) == []
