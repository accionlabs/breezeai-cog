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
from breezeai_cog.parsers.scala_play.parser import PlayRoutesParser, ScalaPlayParser
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


def test_action_and_action_async_emit_one_route_each(tmp_path: Path) -> None:
    rec = _parse(tmp_path, CONTROLLER_SRC, "HomeController.scala")
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    assert "index" in routes
    assert "create" in routes
    assert "notAnAction" not in routes
    assert rec.framework == "play"


def test_action_route_has_honest_null_method_and_endpoint(tmp_path: Path) -> None:
    # This is the test that stops someone "improving" it later — the verb/path live
    # in conf/routes, not the controller; guessing one from the method name is wrong.
    rec = _parse(tmp_path, CONTROLLER_SRC, "HomeController.scala")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert routes
    for r in routes:
        assert r.method is None
        assert r.endpoint is None


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


def test_mount_include_has_null_method(tmp_path: Path) -> None:
    rec = _parse_routes(tmp_path)
    mounts = {(s.endpoint, s.handler): s for s in rec.statements if s.routeKind == "mount"}
    assert ("/v1/posts", "v1.post.PostRouter") in mounts
    assert ("/webjars", "webjars.Routes") in mounts
    assert all(s.method is None for s in mounts.values())


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
