"""Play framework parser: controller action detection (honest-null verb/path) and
``conf/routes`` line parsing. See Spec/220/plan-P2-play-akka-BREEZEAI-220.md."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.detection.db_queries import match_db
from breezeai_cog.parsers.scala.parser import ScalaParser
from breezeai_cog.parsers.scala_play.actions import detect_play_actions
from breezeai_cog.parsers.scala_play.parser import PlayRoutesParser, ScalaPlayParser
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.schemas import FileRecord

CONTROLLER_SRC = b'''package controllers
import play.api.mvc._

class HomeController extends Controller {
  def index = Action { implicit request =>
    Ok("hi")
  }
  def create = Action.async(parse.json) { request =>
    Future.successful(Ok)
  }
  def notAnAction = 42
}
'''

PLAIN_SRC = b"package x\nclass Plain { def add(a: Int): Int = a + 1 }\n"


def _parse(tmp_path: Path, src: bytes, name: str, *, capture: bool = True) -> FileRecord:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path,
                        capture_statements=capture)
    return ScalaPlayParser().parse_file(ctx)


def test_claims_play_only() -> None:
    p = ScalaPlayParser()
    assert p.claims("X.scala", CONTROLLER_SRC) is True
    assert p.claims("X.scala", PLAIN_SRC) is False


def _actions(src: bytes, tmp_path: Path, name: str) -> set[str]:
    rec = _parse(tmp_path, src, name)
    root = parse_source("scala", src, 0).root_node
    return {fn.name for fn in detect_play_actions(root, src, rec)}


def test_actions_are_detected_and_label_the_file(tmp_path: Path) -> None:
    rec = _parse(tmp_path, CONTROLLER_SRC, "HomeController.scala")
    assert _actions(CONTROLLER_SRC, tmp_path, "HomeController.scala") == {"index", "create"}
    assert rec.framework == "play"


def test_action_is_a_handler_not_an_entry_point(tmp_path: Path) -> None:
    # The verb/path live in conf/routes, which records the binding with the action as its
    # handler. A record for the action itself would count each binding twice, with no address.
    rec = _parse(tmp_path, CONTROLLER_SRC, "HomeController.scala")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


def test_action_return_type_does_not_match_action_helpers(tmp_path: Path) -> None:
    src = b'''package controllers
import play.api.mvc._
class Helpers {
  def build(): ActionBuilder[Request, AnyContent] = ???
  def factory(): ActionFunction[Request, Request] = ???
  def result(): ActionResult = ???
  def real(): Action = ???
}
'''
    assert _actions(src, tmp_path, "Helpers.scala") == {"real"}


def test_sird_routes_are_captured(tmp_path: Path) -> None:
    src = b'''package routers
import play.api.routing.Router.Routes
import play.api.routing.SimpleRouter
import play.api.routing.sird._
class SimpleRouterImpl extends SimpleRouter {
  override def routes: Routes = {
    case GET(p"/") => controller.index
    case GET(p"/users/$id") => controller.show(id)
    case GET(p"/items/$id<[0-9]+>") => controller.item(id)
  }
}
'''
    rec = _parse(tmp_path, src, "SimpleRouter.scala")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert {(s.method, s.endpoint) for s in routes} == {
        ("GET", "/"),
        ("GET", "/users/{id}"),
        ("GET", "/items/{id}"),
    }
    assert {s.handler for s in routes} == {"index", "show", "item"}
    assert sum(s.isRegex is True for s in routes) == 1


def test_fixture_controller_emits_no_routes(tmp_path: Path) -> None:
    rec = _parse(tmp_path, CONTROLLER_SRC, "HomeController.test.scala")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


def test_output_validates(tmp_path: Path) -> None:
    rec = _parse(tmp_path, CONTROLLER_SRC, "HomeController.scala")
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_claims_selects_scala_play() -> None:
    registry.clear()
    registry.register(ScalaParser())
    registry.register(ScalaPlayParser())
    sel1 = registry.select("X.scala", CONTROLLER_SRC)
    assert sel1 is not None and sel1.name == "scala-play"
    sel2 = registry.select("X.scala", PLAIN_SRC)
    assert sel2 is not None and sel2.name == "scala"  # plain Scala -> base
    registry.clear()


# --------------------------------------------------------------------------- conf/routes

ROUTES_TEXT = """# a comment, and a blank line follow

GET   /                       controllers.HomeController.index
GET   /                       controllers.HomeController.index()
GET   /computers              controllers.HomeController.list(p:Int ?= 0, s ?= "name", f ?= "")
GET   /computers/:id          controllers.HomeController.edit(request: Request, id:Long)
GET   /assets/*file           controllers.Assets.versioned(path="/public", file: Asset)
GET   /favicon.ico            controllers.Assets.at(path="/images", file ="favicon.png")
->    /v1/posts               v1.post.PostRouter
->    /webjars                webjars.Routes
GET   /users/$id<[0-9]+>      controllers.UserController.show(id:Long)
+ nocsrf
POST  /login                  controllers.AuthController.login
"""


def _parse_routes(tmp_path: Path, text: str = ROUTES_TEXT, name: str = "conf/routes") -> FileRecord:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    ctx = ParseContext(path=name, abs_path=p, source=text.encode(), repo_root=tmp_path,
                        capture_statements=True)
    return PlayRoutesParser().parse_file(ctx)


def test_matches_only_conf_routes() -> None:
    p = PlayRoutesParser()
    assert p.matches("app/conf/routes") is True
    assert p.matches("config/routes") is False  # Rails-style config/routes
    assert p.matches("src/routes") is False


def test_verified_line_shapes_parse(tmp_path: Path) -> None:
    rec = _parse_routes(tmp_path)
    got = {(s.method, s.endpoint, s.handler) for s in rec.statements}
    assert ("GET", "/", "controllers.HomeController.index") in got  # bare and ()
    assert ("GET", "/computers", "controllers.HomeController.list") in got
    assert ("GET", "/computers/:id", "controllers.HomeController.edit") in got
    assert ("GET", "/assets/*file", "controllers.Assets.versioned") in got
    assert ("GET", "/favicon.ico", "controllers.Assets.at") in got
    assert ("POST", "/login", "controllers.AuthController.login") in got


def test_include_of_a_router_not_in_the_routes_files_is_a_mount(tmp_path: Path) -> None:
    # `-> /v1/posts v1.post.PostRouter` names no routes file of the repo: a route / mount record,
    # every verb. `-> /webjars webjars.Routes` serves the WebJars assets: a GET route.
    rec = _parse_routes(tmp_path)
    got = {(s.endpoint, s.method, s.handler, s.routeKind) for s in rec.statements
           if s.text.startswith("->")}
    assert got == {("/v1/posts", "ANY", "v1.post.PostRouter", "mount"),
                   ("/webjars", "GET", None, "route")}


def test_regex_path_part_sets_is_regex(tmp_path: Path) -> None:
    rec = _parse_routes(tmp_path)
    by_endpoint = {s.endpoint: s for s in rec.statements}
    assert by_endpoint["/users/$id<[0-9]+>"].isRegex is True
    assert by_endpoint["/computers/:id"].isRegex is False


def test_comments_blanks_and_modifiers_emit_nothing(tmp_path: Path) -> None:
    rec = _parse_routes(tmp_path, "# only a comment\n\n+ nocsrf\n")
    assert rec.statements == []


def test_routes_record_is_config_scala(tmp_path: Path) -> None:
    rec = _parse_routes(tmp_path)
    assert rec.type == "config"
    assert rec.language == "scala"


def test_routes_requires_capture_statements(tmp_path: Path) -> None:
    p = tmp_path / "conf/routes"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("GET / controllers.HomeController.index\n")
    ctx = ParseContext(path="conf/routes", abs_path=p, source=p.read_bytes(),
                        repo_root=tmp_path, capture_statements=False)
    rec = PlayRoutesParser().parse_file(ctx)
    assert rec.statements == []

def test_routes_output_validates(tmp_path: Path) -> None:
    rec = _parse_routes(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


# --------------------------------------------------------------------------- Slick/Doobie/Quill

def test_slick_doobie_quill_classify() -> None:
    assert match_db("q.forceInsert(x)", "forceInsert") == "slick"
    assert match_db("q.insertOrUpdate(x)", "insertOrUpdate") == "slick"
    assert match_db("TableQuery[Users]", "TableQuery") == "slick"
    assert match_db("sql.transact(xa)", "transact") == "doobie"
    assert match_db("q.liftQuery(xs)", "liftQuery") == "quill"


def test_false_positive_guard_result_and_to() -> None:
    # `.result`/`.to` collide with Future.result / collection .to(List) and are
    # deliberately excluded from the Slick vocabulary — precision over recall.
    assert match_db("future.result", "result") is None
    assert match_db("xs.to", "to") is None


NON_P_SIRD_SRC = b'''package routers
import play.api.routing.SimpleRouter
import play.api.routing.sird._
class FakeRouter extends SimpleRouter {
  override def routes = {
    case GET(s"/interp/$x") => controller.a
    case GET(q"/other") => controller.b
  }
}
'''

PREFIX_ROUTER_SRC = b'''package routers
import play.api.routing.SimpleRouter
import play.api.routing.sird._
class PostRouter extends SimpleRouter {
  val prefix = "/v1/posts"
  override def routes = {
    case GET(p"/") => controller.index
    case GET(p"/$id") => controller.show(id)
  }
}
'''


def test_non_p_interpolators_rejected(tmp_path: Path) -> None:
    rec = _parse(tmp_path, NON_P_SIRD_SRC, "FakeRouter.scala")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 0


def test_sird_inferred_routes_and_mount_prefix(tmp_path: Path) -> None:
    rec = _parse(tmp_path, PREFIX_ROUTER_SRC, "PostRouter.scala")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 2
    endpoints = {(r.method, r.endpoint, r.handler) for r in routes}
    assert endpoints == {
        ("GET", "/v1/posts/", "index"),
        ("GET", "/v1/posts/{id}", "show"),
    }
