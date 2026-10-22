"""Play role table: docs/entry-point-roles/play.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

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
        if is_entry_point(s)
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


def _served(stmts) -> list[tuple[str, str]]:
    return sorted((s.path, s.endpoint) for s in _eps(stmts))


def test_included_routes_file_is_served_under_the_include_prefix(analyze_files) -> None:
    st = analyze_files(
        {
            "conf/routes": "->      /admin           admin.Routes\n",
            "conf/admin.routes": "GET     /users     controllers.Admin.users\n"
            "GET     /          controllers.Admin.index\n",
        }
    )
    assert _served(st) == [("conf/admin.routes", "/admin"), ("conf/admin.routes", "/admin/users")]
    # The include is wiring: its prefix is on the included routes, so it has no record.
    assert not [s for s in st if s.path == "conf/routes" and s.semanticType == "route"]


def test_include_of_an_empty_routes_file_keeps_a_mount_record(analyze_files) -> None:
    # conf/admin.routes has no binding: the include is the only record of /admin.
    st = analyze_files({"conf/routes": "->      /admin           admin.Routes\n",
                        "conf/admin.routes": "# nothing yet\n"})
    got = [(s.endpoint, s.routeKind) for s in st if s.path == "conf/routes"]
    assert got == [("/admin", "mount")]


def test_grpc_router_include_keeps_a_mount_record(analyze_files) -> None:
    # A generated gRPC router is not in the repo's sources: the include is its only record.
    st = analyze_files({"conf/routes": "->      /                routers.HelloWorldRouter\n"})
    got = [(s.endpoint, s.method, s.handler, s.routeKind) for s in st if s.path == "conf/routes"]
    assert got == [("/", "ANY", "routers.HelloWorldRouter", "mount")]


def test_includes_chain(analyze_files) -> None:
    st = analyze_files(
        {
            "conf/routes": "->      /admin           admin.Routes\n",
            "conf/admin.routes": "->      /reports   reports.Routes\n",
            "conf/reports.routes": "GET     /daily     controllers.Reports.daily\n",
        }
    )
    assert _served(st) == [("conf/reports.routes", "/admin/reports/daily")]


def test_router_included_twice_keeps_its_paths(analyze_files) -> None:
    st = analyze_files(
        {
            "conf/routes": "->      /admin           admin.Routes\n"
            "->      /backoffice      admin.Routes\n",
            "conf/admin.routes": "GET     /users     controllers.Admin.users\n",
        }
    )
    assert _served(st) == [("conf/admin.routes", "/users")]


def test_routes_file_not_included_keeps_its_paths(analyze_files) -> None:
    st = analyze_files({"conf/admin.routes": "GET     /users     controllers.Admin.users\n"})
    assert _served(st) == [("conf/admin.routes", "/users")]


SIRD_ROUTER = """package v1.post
import javax.inject.Inject
import play.api.routing.Router.Routes
import play.api.routing.SimpleRouter
import play.api.routing.sird._
class PostRouter @Inject()(controller: PostController) extends SimpleRouter {
  override def routes: Routes = {
    case GET(p"/") => controller.index
    case GET(p"/$id") => controller.show(id)
  }
}
"""


def test_included_sird_router_is_served_under_the_include_prefix(analyze_files) -> None:
    st = analyze_files(
        {
            "conf/routes": "->      /v1/posts        v1.post.PostRouter\n",
            "app/v1/post/PostRouter.scala": SIRD_ROUTER,
        }
    )
    assert _served(st) == [
        ("app/v1/post/PostRouter.scala", "/v1/posts"),
        ("app/v1/post/PostRouter.scala", "/v1/posts/{id}"),
    ]


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


JAVA_CONTROLLER = """package controllers;
import play.mvc.*;
public class HomeController extends Controller {
    public Result index() { return ok("x"); }
}
"""


def _route(st, path: str = "conf/routes"):
    [r] = _eps(st, path)
    return r.handler, r.handlerPath, r.handlerLine


def test_routes_line_leads_to_a_java_controller(analyze_files) -> None:
    st = analyze_files({"conf/routes": "GET  /  controllers.HomeController.index()\n",
                        "app/controllers/HomeController.java": JAVA_CONTROLLER})
    assert _route(st) == ("index", "app/controllers/HomeController.java", 4)


def test_controller_not_in_the_repo_keeps_the_handler_as_written(analyze_files) -> None:
    # Play's own Assets controller: no file to lead to, no guess.
    st = analyze_files({"conf/routes": "GET  /assets/*file  controllers.Assets.at(file)\n"})
    assert _route(st) == ("controllers.Assets.at", None, None)


def test_module_controller_found_by_its_package_not_its_folder(analyze_files) -> None:
    # A sub-project's controller in package `controllers.one`, kept in app/controllers/.
    controller = CONTROLLER.replace("package controllers", "package controllers.one")
    st = analyze_files({
        "modules/one/conf/one.routes": "GET  /orders/:id  controllers.one.OrderController.get(id: Long)\n",
        "modules/one/app/controllers/OrderController.scala": controller,
        "app/controllers/OrderController.scala": CONTROLLER,  # another package: not this one
    })
    assert _route(st, "modules/one/conf/one.routes") == (
        "get", "modules/one/app/controllers/OrderController.scala", 5)


def test_controller_in_another_package_is_not_the_handler(analyze_files) -> None:
    st = analyze_files({
        "conf/routes": "GET  /orders/:id  controllers.admin.OrderController.get(id: Long)\n",
        "app/controllers/OrderController.scala": CONTROLLER,
    })
    assert _route(st) == ("controllers.admin.OrderController.get", None, None)


def test_overloaded_action_has_no_location(analyze_files) -> None:
    controller = CONTROLLER.replace("  def create()", "  def get(id: String) = Action { Ok(\"y\") }\n  def create()")
    st = analyze_files({
        "conf/routes": "GET  /orders/:id  controllers.OrderController.get(id: Long)\n",
        "app/controllers/OrderController.scala": controller,
    })
    assert _route(st) == ("controllers.OrderController.get", None, None)
