"""http4s case-pattern route detection tests + schema validation."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.scala.parser import ScalaParser
from breezeai_cog.parsers.scala_http4s.parser import ScalaHttp4sParser
from breezeai_cog.schemas import FileRecord

HTTP4S_SRC = b"""package com.example.users

import cats.effect._
import org.http4s._
import org.http4s.dsl.io._

class UserRoutes {
  val routes = HttpRoutes.of[IO] {
    case GET -> Root / "users" / id =>
      Ok(id)
    case req @ POST -> Root / "api" / "v1" / "items" =>
      Created("ok")
    case DELETE -> Root / "items" / IntVar(itemId) =>
      Ok()
    case req @ PUT -> Root / "users" / userId / "roles" / roleId =>
      Ok()
    case _ =>
      NotFound()
  }

  val authed = AuthedRoutes.of[User, IO] {
    case GET -> Root / "me" as user =>
      Ok(user.name)
  }
}
"""

PLAIN_SRC = b"""package com.example
class PlainScala {
  def get: String = "not http4s"
}
"""


def _parse_http4s(tmp_path: Path, src: bytes = HTTP4S_SRC, name: str = "UserRoutes.scala") -> FileRecord:
    parser = ScalaHttp4sParser()
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src)
    ctx = ParseContext(
        path=name,
        abs_path=p,
        source=src,
        repo_root=tmp_path,
        capture_statements=True,
        statement_text_limit=200,
    )
    return parser.parse_file(ctx)


def test_http4s_claims() -> None:
    registry.clear()
    registry.register(ScalaParser())
    registry.register(ScalaHttp4sParser())
    sel1 = registry.select("UserRoutes.scala", HTTP4S_SRC)
    assert sel1 is not None and sel1.name == "scala-http4s"
    sel2 = registry.select("Plain.scala", PLAIN_SRC)
    assert sel2 is not None and sel2.name == "scala"  # no org.http4s -> base parser
    registry.clear()


def test_http4s_route_extraction(tmp_path: Path) -> None:
    rec = _parse_http4s(tmp_path)
    assert rec.framework == "http4s"
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 5

    by_ep_verb = {(r.endpoint, r.method): r for r in routes}

    # Case 5: GET -> Root / "users" / id
    r_get_user = by_ep_verb.get(("/users/{id}", "GET"))
    assert r_get_user is not None
    assert r_get_user.routeKind == "route"
    assert r_get_user.framework == "http4s"
    assert r_get_user.handler is None
    assert r_get_user.isRegex is False

    # Case 6: Literal segment stays literal
    r_post_items = by_ep_verb.get(("/api/v1/items", "POST"))
    assert r_post_items is not None

    # Extractor IntVar(itemId) becomes {itemId}
    r_delete_item = by_ep_verb.get(("/items/{itemId}", "DELETE"))
    assert r_delete_item is not None

    # Multiple segments
    r_put_roles = by_ep_verb.get(("/users/{userId}/roles/{roleId}", "PUT"))
    assert r_put_roles is not None

    # AuthedRoutes with `as user`
    r_me = by_ep_verb.get(("/me", "GET"))
    assert r_me is not None

    # Case 7: case _ => NotFound() is skipped (not a route)
    for r in routes:
        assert r.method in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


def test_http4s_schema_validation(tmp_path: Path) -> None:
    rec = _parse_http4s(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
