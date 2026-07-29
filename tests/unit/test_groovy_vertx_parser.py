"""Groovy Vert.x parser: RouteMatcher route detection (receiver-qualified ``route.get`` and
bare ``rm.with { get() }``), GString path rendering, event-bus detection, capture gating,
selection via claims, and schema validity. Plus an integration pass over the real P3
webapp-engine API modules (employee-api / filter-api) when they are checked out locally."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.groovy_vertx.parser import GroovyVertxParser
from breezeai_cog.schemas import FileRecord

# ── receiver-qualified idiom: employee-api's HttpServerVerticle ────────────────────────
EMPLOYEE_SRC = b'''package jp.co.payroll.p3.employee

import org.vertx.groovy.platform.Verticle
import org.vertx.groovy.core.http.RouteMatcher

public class HttpServerVerticle extends Verticle {

  def start() {
    def server = vertx.createHttpServer().requestHandler( routeMatcher() )
    server.listen(config.port)
  }

  private Closure routeMatcher() {
    def prefix = "/api/employee"
    def route = new RouteMatcher()
    route.get("${prefix}/:platformId/empno/issue" , new EmployeeServiceHandler(EmployeeVerticle.ADDRESS_CREATE_EMPNO).&handle)
    route.post("${prefix}/:platformId/employee" , new CreateEmployeeServiceHandler(EmployeeVerticle.ADDRESS_CREATE_EMPNOENTRY).&handle)
    route.put("${prefix}/:platformId/employee" , new CreateEmployeeServiceHandler(EmployeeVerticle.ADDRESS_UPDATE_EMPDATA).&handle)
    route.get("${prefix}/:platformId/empno" , new EmployeeServiceHandler(EmployeeVerticle.ADDRESS_GET_EMPNO).&handle)
    route.get("${prefix}/:platformId/paynumber" , new EmployeeServiceHandler(EmployeeVerticle.ADDRESS_GET_PAYNO).&handle)
    route.get("${prefix}/echo", new EchoHandler(config).&handle)
    route.delete("${prefix}/cache", this.&deleteCache)
    return route.asClosure()
  }
}
'''

# ── bare-call idiom inside a RouteMatcher scope: filter-api's URIMapperVerticle ─────────
FILTER_SRC = b'''package jp.co.payroll.p3.filter

import org.vertx.groovy.core.http.RouteMatcher

public class URIMapperVerticle extends Verticle {

  def routeMatcher() {
    def config = container.config
    def rm = new RouteMatcher()
    rm.with {
      get("${config.'path'}/:platformId/:filterId", new GetFilterDataHandler().asClosure())
      get("${config.'path'}/${ECHO}", getEchoData)
      delete("${config.'path'}/${cache}", deleteCache)
      delete("${config.'path'}/${cache}/schema", deleteCacheResources)
    }
    return rm.asClosure()
  }
}
'''


def _parse(tmp_path, src: bytes, rel: str, *, capture: bool = True) -> FileRecord:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src)
    parser = GroovyVertxParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.groovy")))
    ctx = ParseContext(path=rel, abs_path=p, source=src, repo_root=tmp_path,
                       resolution_index=index, capture_statements=capture)
    return parser.parse_file(ctx)


def _routes(rec: FileRecord) -> set[tuple[str | None, str | None]]:
    return {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}


def test_receiver_qualified_routematcher(tmp_path) -> None:
    """``route.get("${prefix}/...")`` → route, GString rendered to ``{prefix}/...``."""
    rec = _parse(tmp_path, EMPLOYEE_SRC, "HttpServerVerticle.groovy")
    assert rec.framework == "vertx"
    routes = _routes(rec)
    assert routes == {
        ("GET", "{prefix}/:platformId/empno/issue"),
        ("POST", "{prefix}/:platformId/employee"),
        ("PUT", "{prefix}/:platformId/employee"),
        ("GET", "{prefix}/:platformId/empno"),
        ("GET", "{prefix}/:platformId/paynumber"),
        ("GET", "{prefix}/echo"),
        ("DELETE", "{prefix}/cache"),
    }
    assert all(s.framework == "vertx" and s.routeKind == "route"
               for s in rec.statements if s.semanticType == "route")


def test_bare_calls_inside_routematcher_scope(tmp_path) -> None:
    """Bare ``get("...")`` / ``delete("...")`` inside ``rm.with {}`` → route (route_scope)."""
    rec = _parse(tmp_path, FILTER_SRC, "URIMapperVerticle.groovy")
    assert rec.framework == "vertx"
    routes = _routes(rec)
    assert routes == {
        ("GET", "{config.path}/:platformId/:filterId"),
        ("GET", "{config.path}/{ECHO}"),
        ("DELETE", "{config.path}/{cache}"),
        ("DELETE", "{config.path}/{cache}/schema"),
    }


def test_bare_verb_calls_need_routematcher_in_file(tmp_path) -> None:
    """Guard: a bare ``get("a/b")`` with no RouteMatcher anywhere is NOT a route."""
    src = b'''package x
import org.vertx.groovy.platform.Verticle
class C extends Verticle {
  def f() {
    def m = [:]
    get("a/b/c")
  }
}
'''
    rec = _parse(tmp_path, src, "C.groovy")
    assert _routes(rec) == set()


def test_routes_require_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, EMPLOYEE_SRC, "HttpServerVerticle.groovy", capture=False)
    assert [s for s in rec.statements if s.semanticType] == []
    assert rec.framework is None


def test_claims_selects_groovy_vertx() -> None:
    registry.clear()
    from breezeai_cog.parsers.groovy.parser import GroovyParser

    registry.register(GroovyParser())
    registry.register(GroovyVertxParser())
    # 2.x org.vertx.groovy root (P3 webapp-engine) must select the Vert.x parser
    assert registry.select("X.groovy", b"import org.vertx.groovy.core.http.RouteMatcher").name == "groovy-vertx"
    assert registry.select("X.groovy", b"import io.vertx.core.Vertx").name == "groovy-vertx"
    assert registry.select("X.groovy", b"package x").name == "groovy"  # plain Groovy -> base
    registry.clear()


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, EMPLOYEE_SRC, "HttpServerVerticle.groovy")
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


# ── integration: the real P3 modules the gap was found in ──────────────────────────────
_P3 = Path("/home/kannan/breeze/client-repos/p3_selected")


def _parse_real(repo: str, rel: str) -> FileRecord:
    abs_path = _P3 / repo / rel
    src = abs_path.read_bytes()
    ctx = ParseContext(path=rel, abs_path=abs_path, source=src, repo_root=_P3 / repo,
                       capture_statements=True)
    return GroovyVertxParser().parse_file(ctx)


@pytest.mark.skipif(
    not (_P3 / "employee-api/src/main/groovy/jp/co/payroll/p3/employee/HttpServerVerticle.groovy").exists(),
    reason="p3_selected client repos not checked out",
)
def test_real_employee_api_routes() -> None:
    rec = _parse_real(
        "employee-api",
        "src/main/groovy/jp/co/payroll/p3/employee/HttpServerVerticle.groovy",
    )
    assert rec.framework == "vertx"
    routes = _routes(rec)
    assert routes == {
        ("GET", "{prefix}/:platformId/empno/issue"),
        ("POST", "{prefix}/:platformId/employee"),
        ("PUT", "{prefix}/:platformId/employee"),
        ("GET", "{prefix}/:platformId/empno"),
        ("GET", "{prefix}/:platformId/paynumber"),
        ("GET", "{prefix}/echo"),
        ("DELETE", "{prefix}/cache"),
    }, routes


@pytest.mark.skipif(
    not (_P3 / "filter-api-java-11/src/main/groovy/jp/co/payroll/p3/filter/URIMapperVerticle.groovy").exists(),
    reason="p3_selected client repos not checked out",
)
def test_real_filter_api_routes() -> None:
    rec = _parse_real(
        "filter-api-java-11",
        "src/main/groovy/jp/co/payroll/p3/filter/URIMapperVerticle.groovy",
    )
    assert rec.framework == "vertx"
    routes = _routes(rec)
    # 3 get + 3 delete registered inside `rm.with { ... }`
    assert ("GET", "{config.path}/:platformId/:filterId") in routes
    assert routes >= {
        ("GET", "{config.path}/:platformId/:filterId"),
        ("GET", "{config.path}/{ECHO}"),
        ("DELETE", "{config.path}/{cache}"),
        ("DELETE", "{config.path}/{cache}/schema"),
    }
    assert sum(1 for m, _ in routes if m == "GET") >= 3
    assert sum(1 for m, _ in routes if m == "DELETE") >= 3
