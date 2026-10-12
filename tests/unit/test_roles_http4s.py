"""http4s role table: docs/entry-point-roles/http4s.md — one test per row."""

from __future__ import annotations

EP = {"route", "eventbus_consumer", "timer"}
SRC = "src/main/scala/a/H.scala"
HEAD = """package a
import cats.effect.IO
import org.http4s._
import org.http4s.dsl.io._
import org.http4s.server.{AuthMiddleware, Router}
"""


def _obj(body: str) -> dict[str, str]:
    """``body`` goes inside ``object H``; its first line is line 7."""
    return {SRC: HEAD + "object H {\n" + body + "\n}\n"}


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


ROUTES = """  val orderRoutes = HttpRoutes.of[IO] {
    case GET -> Root / "orders" / id => Ok(id)
    case req @ POST -> Root / "orders" => Ok("c")
  }"""


def test_case_in_http_routes_is_binding(analyze_files) -> None:
    st = analyze_files(_obj(ROUTES))
    got = {(r.method, r.endpoint, r.routeKind, r.framework) for r in _eps(st)}
    assert got == {
        ("GET", "/orders/{id}", "route", "http4s"),
        ("POST", "/orders", "route", "http4s"),
    }


def test_case_records_anonymous_handler(analyze_files) -> None:
    st = analyze_files(_obj(ROUTES))
    got = {(r.method, r.handler, r.handlerLine) for r in _eps(st)}
    assert got == {("GET", None, 8), ("POST", None, 9)}


def test_case_in_authed_routes_is_binding(analyze_files) -> None:
    st = analyze_files(
        _obj("""  val authed = AuthedRoutes.of[User, IO] {
    case GET -> Root / "me" as user => Ok(user.name)
  }""")
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint) == ("GET", "/me")


def test_router_mount_is_wiring(analyze_files) -> None:
    st = analyze_files(
        _obj(
            ROUTES
            + """
  val app = Router("/api" -> orderRoutes)"""
        )
    )
    eps = _eps(st)
    assert 11 not in {r.startLine for r in eps}  # the Router(...) line has no record
    assert {r.endpoint for r in eps} == {"/api/orders/{id}", "/api/orders"}


def test_auth_middleware_is_guard(analyze_files) -> None:
    st = analyze_files(
        _obj("""  val authed = AuthedRoutes.of[User, IO] {
    case GET -> Root / "me" as user => Ok(user.name)
  }
  val middleware: AuthMiddleware[IO, User] = AuthMiddleware(authUser)
  val routes = middleware(authed)""")
    )
    [r] = _eps(st)
    assert r.guards


def test_server_builder_is_none(analyze_files) -> None:
    st = analyze_files(
        _obj("""  val server = EmberServerBuilder.default[IO].withHttpApp(app).build""")
    )
    assert _eps(st) == []


def test_client_call_is_not_entry_point(analyze_files) -> None:
    st = analyze_files(
        _obj("""  def fetch(client: org.http4s.client.Client[IO]) =
    client.expect[String]("http://billing/api/x")""")
    )
    assert _eps(st) == []
