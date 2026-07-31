"""Groovy Vert.x parser: RouteMatcher route detection (receiver-qualified ``route.get`` and
bare ``rm.with { get() }``), GString path rendering, event-bus detection, capture gating,
selection via claims, and schema validity."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.groovy_vertx.parser import GroovyVertxParser
from breezeai_cog.schemas import FileRecord

# ── receiver-qualified idiom: route.get/post/put/delete on a RouteMatcher ──────────────
EMPLOYEE_SRC = b'''package com.example.web

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

# ── bare-call idiom: get/delete inside an ``rm.with { ... }`` scope ─────────────────────
FILTER_SRC = b'''package com.example.web

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


def test_constant_addresses_fold(tmp_path) -> None:
    # A `static final String` address folds to its value (same-file and cross-file); a runtime
    # variable has no compile-time value and stays symbolic.
    (tmp_path / "Addr.groovy").write_text(
        "package a\nclass Addr { public static final String WEB = 'svc/web' }\n"
    )
    src = (
        b"package a\n"
        b"import org.vertx.groovy.platform.Verticle\n"
        b"class N extends Verticle {\n"
        b"  static final String LOCAL = 'svc/local'\n"
        b"  def start() {\n"
        b"    eb.registerHandler(LOCAL, h)\n"
        b"    eb.registerHandler(Addr.WEB, h)\n"
        b"    eb.registerHandler(runtimeVar, h)\n"
        b"  }\n}\n"
    )
    rec = _parse(tmp_path, src, "N.groovy")
    eps = {s.endpoint for s in rec.statements if s.semanticType == "eventbus_consumer"}
    assert "svc/local" in eps    # same-file constant
    assert "svc/web" in eps      # cross-file constant (Addr.WEB)
    assert "runtimeVar" in eps   # unresolvable var → symbol


def test_dynamic_prefix_route_renders_placeholder(tmp_path) -> None:
    # A route path that concatenates a runtime variable with a literal (`cfg.path + '/job'`)
    # renders the runtime part as a placeholder — the route surface stays visible.
    src = (
        b"package a\n"
        b"import org.vertx.groovy.core.http.RouteMatcher\n"
        b"class M extends Verticle {\n"
        b"  def routes() {\n"
        b"    def router = new RouteMatcher()\n"
        b"    router.post(cfg.path + '/job', h)\n"
        b"    router.delete(cfg.path + '/job/:jobId', h)\n"
        b"  }\n}\n"
    )
    rec = _parse(tmp_path, src, "M.groovy")
    routes = _routes(rec)
    assert ("POST", "{path}/job") in routes
    assert ("DELETE", "{path}/job/:jobId") in routes


def test_routes_require_capture_statements(tmp_path) -> None:
    rec = _parse(tmp_path, EMPLOYEE_SRC, "HttpServerVerticle.groovy", capture=False)
    assert [s for s in rec.statements if s.semanticType] == []
    assert rec.framework is None


def test_claims_selects_groovy_vertx() -> None:
    registry.clear()
    from breezeai_cog.parsers.groovy.parser import GroovyParser

    registry.register(GroovyParser())
    registry.register(GroovyVertxParser())
    # 2.x org.vertx.groovy root must select the Vert.x parser
    assert registry.select("X.groovy", b"import org.vertx.groovy.core.http.RouteMatcher").name == "groovy-vertx"
    assert registry.select("X.groovy", b"import io.vertx.core.Vertx").name == "groovy-vertx"
    assert registry.select("X.groovy", b"package x").name == "groovy"  # plain Groovy -> base
    registry.clear()


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, EMPLOYEE_SRC, "HttpServerVerticle.groovy")
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
