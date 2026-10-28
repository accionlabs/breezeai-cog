"""Groovy Vert.x parser: RouteMatcher route detection (receiver-qualified ``route.get`` and
bare ``rm.with { get() }``), GString path rendering, event-bus detection, capture gating,
selection via claims, and schema validity."""

from __future__ import annotations

import pytest

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
    """``route.get("${prefix}/...")`` → route; ``def prefix = "/api/employee"`` (bound once)
    is substituted into the GString."""
    rec = _parse(tmp_path, EMPLOYEE_SRC, "HttpServerVerticle.groovy")
    assert rec.framework == "vertx"
    routes = _routes(rec)
    assert routes == {
        ("GET", "/api/employee/:platformId/empno/issue"),
        ("POST", "/api/employee/:platformId/employee"),
        ("PUT", "/api/employee/:platformId/employee"),
        ("GET", "/api/employee/:platformId/empno"),
        ("GET", "/api/employee/:platformId/paynumber"),
        ("GET", "/api/employee/echo"),
        ("DELETE", "/api/employee/cache"),
    }
    assert all(s.framework == "vertx" and s.routeKind == "route"
               for s in rec.statements if s.semanticType == "route")


def test_bare_calls_inside_routematcher_scope(tmp_path) -> None:
    """Bare ``get("...")`` / ``delete("...")`` inside ``rm.with {}`` → route (route_scope). The
    paths start with ``${config.path}`` (runtime config): the address is unknown → empty."""
    rec = _parse(tmp_path, FILTER_SRC, "URIMapperVerticle.groovy")
    assert rec.framework == "vertx"
    routes = [(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"]
    assert sorted(m for m, _ in routes) == ["DELETE", "DELETE", "GET", "GET"]
    assert all(ep is None for _, ep in routes)


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
    assert None in eps and "runtimeVar" not in eps  # unresolvable var → empty (Q15)


def test_dynamic_prefix_route_leaves_endpoint_empty(tmp_path) -> None:
    # A route path that concatenates a runtime variable with a literal (`cfg.path + '/job'`) is
    # still a route, but its address is unknown: endpoint empty — never a {path} placeholder.
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
    assert ("POST", None) in routes
    assert ("DELETE", None) in routes


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


_HANDLERS = b"""import io.vertx.ext.web.Router

class Api {
    void start() {
        def router = Router.router(vertx)
        router.get("/a").handler(this::list)
        vertx.eventBus().consumer("q.one", { m -> m.reply(1) })
        vertx.eventBus().consumer("q.two", this.&onTwo)
    }

    void list(ctx) {}
    void onTwo(m) {}
}
"""


def test_route_and_consumer_handlers(tmp_path) -> None:
    rec = _parse(tmp_path, _HANDLERS, "src/Api.groovy")
    got = {s.endpoint: (s.handler, s.handlerLine) for s in rec.statements
           if s.semanticType in ("route", "eventbus_consumer")}
    assert got == {
        "/a": ("list", 11),        # this::list — declared in this class
        "q.one": (None, 7),        # closure: no name, its line
        "q.two": ("onTwo", 12),    # this.&onTwo method pointer
    }


_SUB_ROUTERS = b"""import io.vertx.ext.web.Router

class Api extends io.vertx.core.AbstractVerticle {
  void start() {
    def router = Router.router(vertx)
    def apiRouter = Router.router(vertx)
    apiRouter.get("/items").handler(this::items)
    router.route("/api/*").subRouter(apiRouter)
  }
}
"""


def test_sub_router_prefix(tmp_path) -> None:
    rec = _parse(tmp_path, _SUB_ROUTERS, "src/Api.groovy")
    got = [(s.endpoint, s.handler) for s in rec.statements if s.semanticType == "route"]
    assert got == [("/api/items", "items")]


def test_gstring_constants_resolve_reassigned_variables_do_not(tmp_path) -> None:
    src = (
        b"package a\n"
        b"import io.vertx.ext.web.Router\n"
        b"class R {\n"
        b"  static final String BASE = '/v1'\n"
        b"  def routes(router) {\n"
        b"    def fixed = '/fixed'\n"
        b"    def moving = '/a'\n"
        b"    moving = '/b'\n"
        b'    router.get("${BASE}/x").handler(h)\n'
        b'    router.get("${fixed}/y").handler(h)\n'
        b'    router.get("${moving}/z").handler(h)\n'
        b"  }\n}\n"
    )
    rec = _parse(tmp_path, src, "R.groovy")
    assert _routes(rec) == {("GET", "/v1/x"), ("GET", "/fixed/y"), ("GET", None)}


def test_local_literal_folds_only_where_it_is_visible(tmp_path) -> None:
    src = (
        b"import io.vertx.ext.web.Router\n"
        b"class Api {\n"
        b"  static final String BASE = '/v1'\n"
        b"  String field = '/f'\n"
        b"  void one(def router) {\n"
        b"    def prefix = '/a'\n"
        b'    router.get("${prefix}/x").handler(h)\n'
        b'    router.get("${field}/w").handler(h)\n'
        b"  }\n"
        b"  void two(def router, String prefix) {\n"
        b'    router.get("${prefix}/y").handler(h)\n'                  # the parameter, not one()'s
        b"  }\n"
        b"  void three(def router, cfg) {\n"
        b"    def BASE = cfg.base\n"
        b'    router.get("${BASE}/v").handler(h)\n'                    # the local hides the constant
        b"    def prefix = '/c'\n"
        b'    def each = { prefix -> router.get("${prefix}/q").handler(h) }\n'  # closure parameter
        b"  }\n}\n"
    )
    rec = _parse(tmp_path, src, "Api.groovy")
    assert _routes(rec) == {("GET", "/a/x"), ("GET", "/f/w"), ("GET", None)}
    assert sorted(s.endpoint or "" for s in rec.statements if s.semanticType == "route") == [
        "", "", "", "/a/x", "/f/w"]


def test_script_local_is_not_seen_inside_a_method(tmp_path) -> None:
    src = (
        b"import io.vertx.ext.web.Router\n"
        b"def prefix = '/s'\n"
        b'router.get("${prefix}/top").handler(h)\n'
        b'def c = { router.get("${prefix}/closure").handler(h) }\n'
        b"void m(router) {\n"
        b'  router.get("${prefix}/method").handler(h)\n'
        b"}\n"
    )
    rec = _parse(tmp_path, src, "script.groovy")
    assert sorted(s.endpoint or "" for s in rec.statements if s.semanticType == "route") == [
        "", "/s/closure", "/s/top"]


_GROOVY_USER_API = """import io.vertx.ext.web.Router
class UserApi {
  static Router router(def vertx) {
    def userRouter = Router.router(vertx)
    userRouter.get("/users").handler({ ctx -> ctx.end() })
    userRouter
  }
}
"""


@pytest.mark.parametrize(("main", "expected"), [
    ('    router.mountSubRouter("/api", UserApi.router(vertx))\n', "/api/users"),
    ("    vertx.createHttpServer().requestHandler(UserApi.router(vertx))\n", "/users"),
    ('    router.mountSubRouter("/a", UserApi.router(vertx))\n'
     '    router.mountSubRouter("/b", UserApi.router(vertx))\n', None),
])
def test_groovy_router_factory_is_served_where_it_is_mounted(analyze_files, main, expected) -> None:
    st = analyze_files({
        "src/UserApi.groovy": _GROOVY_USER_API,
        "src/Main.groovy": ("import io.vertx.ext.web.Router\nclass Main extends io.vertx.core.AbstractVerticle {\n"
                            "  void start() {\n    def router = Router.router(vertx)\n" + main + "  }\n}\n"),
    })
    got = [s.endpoint for s in st if s.semanticType == "route" and s.path == "src/UserApi.groovy"]
    assert got == [expected]


def test_groovy_field_sub_router_and_mount_route(tmp_path) -> None:
    src = b"""import io.vertx.ext.web.Router
class Api extends io.vertx.core.AbstractVerticle {
  def adminRouter
  void start() {
    def router = Router.router(vertx)
    adminRouter = Router.router(vertx)
    adminRouter.get("/stats").handler(this::stats)
    router.route("/admin/*").subRouter(this.adminRouter)
  }
}
"""
    rec = _parse(tmp_path, src, "src/Api.groovy")
    assert _routes(rec) == {("GET", "/admin/stats")}  # no record for the mount route itself
