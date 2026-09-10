"""Akka HTTP directive DSL route detection tests + schema validation."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.scala.parser import ScalaParser
from breezeai_cog.parsers.scala_akkahttp.parser import ScalaAkkaHttpParser
from breezeai_cog.schemas import FileRecord

AKKA_HTTP_SRC = b"""package com.example.orders

import akka.http.scaladsl.server.Directives._
import akka.http.scaladsl.server.Route

class OrderRoutes {
  val route: Route =
    pathPrefix("api" / "v1") {
      path("orders") {
        get {
          complete("orders list")
        } ~
        post {
          complete("order created")
        }
      } ~
      pathPrefix("users" / Segment) { userId =>
        path("profile") {
          get {
            complete(userId)
          }
        }
      } ~
      path("raw") {
        extractRequest { req =>
          complete("done")
        }
      }
    }
}
"""

PLAIN_SRC = b"""package com.example
class PlainScala {
  def get: String = "not akka http"
}
"""


def _parse_akka_http(tmp_path: Path, src: bytes = AKKA_HTTP_SRC, name: str = "OrderRoutes.scala") -> FileRecord:
    parser = ScalaAkkaHttpParser()
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


def test_akkahttp_claims() -> None:
    registry.clear()
    registry.register(ScalaParser())
    registry.register(ScalaAkkaHttpParser())
    sel1 = registry.select("OrderRoutes.scala", AKKA_HTTP_SRC)
    assert sel1 is not None and sel1.name == "scala-akka-http"
    sel2 = registry.select("Plain.scala", PLAIN_SRC)
    assert sel2 is not None and sel2.name == "scala"  # no akka.http.scaladsl -> base parser
    registry.clear()


def test_akkahttp_route_extraction(tmp_path: Path) -> None:
    rec = _parse_akka_http(tmp_path)
    assert rec.framework == "akka-http"
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 4

    by_ep_verb = {(r.endpoint, r.method): r for r in routes}

    # Case 1 & 2: nested pathPrefix + path with get/post
    r_get_orders = by_ep_verb.get(("/api/v1/orders", "GET"))
    assert r_get_orders is not None
    assert r_get_orders.routeKind == "route"
    assert r_get_orders.framework == "akka-http"
    assert r_get_orders.handler is None
    assert r_get_orders.isRegex is False

    r_post_orders = by_ep_verb.get(("/api/v1/orders", "POST"))
    assert r_post_orders is not None

    # Path with lambda variable segment substitution
    r_get_user = by_ep_verb.get(("/api/v1/users/{userId}/profile", "GET"))
    assert r_get_user is not None

    # Case 3: directive block with no HTTP verb directive -> honest null method
    r_raw = by_ep_verb.get(("/api/v1/raw", None))
    assert r_raw is not None
    assert r_raw.method is None


def test_akkahttp_simple_path(tmp_path: Path) -> None:
    src = b"""package com.example
import akka.http.scaladsl.server.Directives._
class Simple {
  val r = path("x") {
    get {
      complete("ok")
    }
  }
}
"""
    rec = _parse_akka_http(tmp_path, src, "Simple.scala")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].endpoint == "/x"
    assert routes[0].method == "GET"


def test_akkahttp_explicit_method_and_bare_verb_guard(tmp_path: Path) -> None:
    src = b'''package com.example
import akka.http.scaladsl.server.Directives._
class Explicit {
  val route = path("legacy") {
    method(HttpMethods.GET) { complete("ok") }
  }
  val notRoute = get { loadConfig() }
  val alsoNotRoute = options { defaultOptions }
  val safe = repo.delete { row => row }
}
'''
    rec = _parse_akka_http(tmp_path, src, "Explicit.scala")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert [(s.method, s.endpoint) for s in routes] == [("GET", "/legacy")]


def test_akkahttp_schema_validation(tmp_path: Path) -> None:
    rec = _parse_akka_http(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
