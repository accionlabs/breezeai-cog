"""Play role table: docs/entry-point-roles/play.md — one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}

CONTROLLER = """package controllers
import javax.inject._
import play.api.mvc._
class OrderController @Inject()(cc: ControllerComponents) extends AbstractController(cc) {
  def get(id: Long) = Action { Ok("x") }
  def create() = Action.async { req => ??? }
}
"""


def _eps(stmts, path: str | None = None):
    return [
        s
        for s in stmts
        if s.semanticType in EP
        and s.routeKind != "field_resolver"
        and (path is None or s.path == path)
    ]


def test_routes_line_is_binding(analyze_files) -> None:
    st = analyze_files(
        {"conf/routes": "GET     /orders/:id      controllers.OrderController.get(id: Long)\n"}
    )
    [r] = _eps(st, "conf/routes")
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.framework) == (
        "route",
        "GET",
        "route",
        "/orders/:id",
        "play",
    )


@pytest.mark.xfail(
    strict=True,
    reason="routes-line handler is the qualified controller path, "
    "not the action name with its file and line",
)
def test_routes_line_records_handler(analyze_files) -> None:
    st = analyze_files(
        {
            "conf/routes": "GET     /orders/:id      controllers.OrderController.get(id: Long)\n",
            "app/controllers/OrderController.scala": CONTROLLER,
        }
    )
    [r] = _eps(st, "conf/routes")
    assert (r.handler, getattr(r, "handlerPath", None), r.handlerLine) == (
        "get",
        "app/controllers/OrderController.scala",
        5,
    )


def test_router_include_is_wiring(analyze_files) -> None:
    st = analyze_files({"conf/routes": "->      /admin           admin.Routes\n"})
    assert _eps(st) == []


def test_controller_action_is_none(analyze_files) -> None:
    st = analyze_files({"app/controllers/OrderController.scala": CONTROLLER})
    assert _eps(st) == []


def test_sird_case_is_binding(analyze_files) -> None:
    st = analyze_files(
        {
            "app/v1/PostRouter.scala": """package v1
import javax.inject.Inject
import play.api.routing.Router.Routes
import play.api.routing.SimpleRouter
import play.api.routing.sird._
class PostRouter @Inject()(controller: PostController) extends SimpleRouter {
  override def routes: Routes = {
    case GET(p"/posts/$id") => controller.show(id)
  }
}
"""
        }
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint, r.routeKind, r.handler) == ("GET", "/posts/{id}", "route", "show")


def test_filter_has_no_record(analyze_files) -> None:
    st = analyze_files(
        {
            "app/filters/LogFilter.scala": """package filters
import javax.inject.Inject
import play.api.mvc._
class LogFilter @Inject()(implicit val mat: akka.stream.Materializer) extends Filter {
  def apply(next: RequestHeader => scala.concurrent.Future[Result])(rh: RequestHeader) = next(rh)
}
"""
        }
    )
    assert _eps(st) == []


def test_ws_client_call_is_not_entry_point(analyze_files) -> None:
    st = analyze_files(
        {
            "app/services/Billing.scala": """package services
import javax.inject.Inject
import play.api.libs.ws._
class Billing @Inject()(ws: WSClient) {
  def fetch() = ws.url("http://billing/api/x").get()
}
"""
        }
    )
    assert _eps(st) == []
