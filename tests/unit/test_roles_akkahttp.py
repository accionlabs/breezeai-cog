"""Akka HTTP role table: docs/entry-point-roles/akka-http.md — one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}
SRC = "src/main/scala/a/R.scala"
HEAD = """package a
import akka.http.scaladsl.server.Directives._
import akka.http.scaladsl.server.Route
import akka.http.scaladsl.Http
"""


def _routes(body: str) -> dict[str, str]:
    """``body`` is the value of ``val route: Route =``; its first line is line 7."""
    return {SRC: HEAD + "object R {\n  val route: Route =\n" + body + "\n}\n"}


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


ORDERS = """    pathPrefix("orders") {
      concat(
        pathEnd {
          concat(
            get { complete("list") },
            post { complete("create") }
          )
        },
        path(Segment) { id =>
          get { complete(id) }
        }
      )
    }"""


def test_verb_directive_is_binding(analyze_files) -> None:
    st = analyze_files(_routes(ORDERS))
    got = {(r.method, r.endpoint, r.routeKind, r.framework) for r in _eps(st)}
    assert got == {
        ("GET", "/orders", "route", "akka-http"),
        ("POST", "/orders", "route", "akka-http"),
        ("GET", "/orders/{id}", "route", "akka-http"),
    }


@pytest.mark.xfail(strict=True, reason="anonymous Akka HTTP handler has no handlerLine")
def test_verb_directive_records_anonymous_handler(analyze_files) -> None:
    st = analyze_files(
        _routes("""    path("health") {
      get { complete("ok") }
    }""")
    )
    [r] = _eps(st)
    assert (r.handler, r.handlerLine) == (None, 8)


def test_path_directive_without_verb_is_binding_for_every_verb(analyze_files) -> None:
    st = analyze_files(
        _routes("""    path("any") {
      complete("ok")
    }""")
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint) == (None, "/any")


def test_path_directives_around_verbs_are_wiring(analyze_files) -> None:
    st = analyze_files(_routes(ORDERS))
    assert 7 not in {r.startLine for r in _eps(st)}  # pathPrefix("orders") has no record


def test_authenticate_directive_has_no_record(analyze_files) -> None:
    st = analyze_files(
        _routes("""    authenticateBasic(realm = "x", myAuth) { user =>
      path("me") {
        get { complete(user) }
      }
    }""")
    )
    [r] = _eps(st)
    assert r.endpoint == "/me"


@pytest.mark.xfail(strict=True, reason="authentication directives are not recorded in guards")
def test_authenticate_directive_is_guard(analyze_files) -> None:
    st = analyze_files(
        _routes("""    authenticateBasic(realm = "x", myAuth) { user =>
      path("me") {
        get { complete(user) }
      }
    }""")
    )
    [r] = _eps(st)
    assert r.guards and "authenticateBasic" in r.guards[0]


def test_server_bind_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            SRC: HEAD
            + """object Main {
  def run(route: Route): Unit = Http().newServerAt("0.0.0.0", 8080).bind(route)
}
"""
        }
    )
    assert _eps(st) == []


def test_client_request_is_not_entry_point(analyze_files) -> None:
    st = analyze_files(
        {
            SRC: HEAD
            + """import akka.http.scaladsl.model.HttpRequest
object Client {
  def fetch() = Http().singleRequest(HttpRequest(uri = "http://billing/api/x"))
}
"""
        }
    )
    assert _eps(st) == []
