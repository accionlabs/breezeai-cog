"""Vert.x-web middleware in a route's ``guards``: path-less registrations made before it on the
same router and method, and Vert.x library handlers chained ahead of its own handler. The app's
own handlers on one path are not guards but steps (``handlerChain``, test_vertx_handler_chains)."""

from __future__ import annotations

SRC = "src/main/java/a/Main.java"
IMPORTS = """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
import io.vertx.ext.web.handler.BodyHandler;
import io.vertx.ext.web.handler.LoggerHandler;
"""


def _handler_class(name: str) -> str:
    return f"""package a;
import io.vertx.core.Handler;
import io.vertx.ext.web.RoutingContext;
public class {name} implements Handler<RoutingContext> {{
  public static {name} create(Object o) {{ return new {name}(); }}
  public void handle(RoutingContext ctx) {{ ctx.next(); }}
}}
"""


def _main(body: str, *handlers: str, imports: str = IMPORTS) -> dict[str, str]:
    """A verticle whose ``start()`` holds ``body``; line 8 is the body's first line."""
    files = {
        SRC: imports
        + f"""public class Main extends AbstractVerticle {{
  public void start() {{
{body}
  }}
  void a(Object c) {{}}
  void b(Object c) {{}}
  void c(Object c) {{}}
}}
"""
    }
    for name in handlers:
        files[f"src/main/java/a/{name}.java"] = _handler_class(name)
    return files


def _routes(stmts):
    return sorted(
        (s for s in stmts if s.path == SRC and s.semanticType == "route"), key=lambda s: s.startLine
    )


def _summary(stmts):
    return [(r.startLine, r.method, r.endpoint, r.handler, r.guards) for r in _routes(stmts)]


def test_registrations_of_one_path_are_one_record_with_its_chain(analyze_files) -> None:
    body = """    Router router = Router.router(vertx);
    router.route().handler(LoggerHandler.create());
    router.post().handler(BodyHandler.create());
    router.post("/claim").handler(SessionHandler.create(null));
    router.post("/claim").handler(SaveHandler.create(null));
    router.post("/claim").failureHandler(this::c);"""
    st = analyze_files(_main(body, "SessionHandler", "SaveHandler"))
    [route] = _routes(st)
    # the path-less middleware stays in guards; the record leads to the first step
    assert (route.startLine, route.handlerPath, route.guards) == (
        11, "src/main/java/a/SessionHandler.java", ["LoggerHandler", "BodyHandler"])
    assert [(s.name, s.handlerPath, s.phase) for s in route.handlerChain] == [
        ("SessionHandler", "src/main/java/a/SessionHandler.java", None),
        ("SaveHandler", "src/main/java/a/SaveHandler.java", None),
        ("c", None, "failure"),
    ]
    assert route.endLine == route.startLine  # the record stays at its first registration


def test_route_middleware_reaches_every_method_registered_after_it(analyze_files) -> None:
    body = """    Router router = Router.router(vertx);
    router.get("/early").handler(this::a);
    router.route().handler(LoggerHandler.create());
    router.get("/x").handler(this::b);
    router.delete("/y").handler(this::c);"""
    assert _summary(analyze_files(_main(body))) == [
        (9, "GET", "/early", "a", None),
        (11, "GET", "/x", "b", ["LoggerHandler"]),
        (12, "DELETE", "/y", "c", ["LoggerHandler"]),
    ]


def test_verb_middleware_applies_to_that_verb_only(analyze_files) -> None:
    body = """    Router router = Router.router(vertx);
    router.post().handler(BodyHandler.create());
    router.get("/x").handler(this::a);
    router.post("/x").handler(this::b);"""
    assert _summary(analyze_files(_main(body))) == [
        (10, "GET", "/x", "a", None),
        (11, "POST", "/x", "b", ["BodyHandler"]),
    ]


def test_a_pathless_registration_has_no_record(analyze_files) -> None:
    body = """    Router router = Router.router(vertx);
    router.route().handler(LoggerHandler.create());"""
    assert _routes(analyze_files(_main(body))) == []


def test_a_library_handler_chained_ahead_is_a_guard(analyze_files) -> None:
    body = """    Router router = Router.router(vertx);
    router.post("/x").handler(BodyHandler.create()).handler(this::a);"""
    assert _summary(analyze_files(_main(body))) == [(9, "POST", "/x", "a", ["BodyHandler"])]


def test_an_app_handler_chained_ahead_is_not_a_guard(analyze_files) -> None:
    # a step of the endpoint, not middleware: the chain holds every step, the record the first
    body = """    Router router = Router.router(vertx);
    router.post("/x").handler(this::a).handler(SessionHandler.create(null)).handler(this::b);"""
    st = analyze_files(_main(body, "SessionHandler"))
    assert _summary(st) == [(9, "POST", "/x", "a", None)]
    assert [s.name for s in _routes(st)[0].handlerChain] == ["a", "SessionHandler", "b"]


def test_order_or_last_on_the_route_drops_the_pathless_guards(analyze_files) -> None:
    # `.order(-1)` runs /health before BodyHandler: registration order is not run order
    body = """    Router router = Router.router(vertx);
    router.route().handler(BodyHandler.create());
    router.get("/health").order(-1).handler(this::a);
    router.get("/tail").last().handler(this::b);
    router.get("/x").handler(this::c);"""
    assert _summary(analyze_files(_main(body))) == [
        (10, "GET", "/health", "a", None),
        (11, "GET", "/tail", "b", None),
        (12, "GET", "/x", "c", ["BodyHandler"]),
    ]


def test_reordered_middleware_is_left_out(analyze_files) -> None:
    body = """    Router router = Router.router(vertx);
    router.route().order(100).handler(LoggerHandler.create());
    router.route().handler(BodyHandler.create());
    router.get("/x").handler(this::a);"""
    assert _summary(analyze_files(_main(body))) == [(11, "GET", "/x", "a", ["BodyHandler"])]


def test_inline_middleware_is_left_out(analyze_files) -> None:
    body = """    Router router = Router.router(vertx);
    router.route().handler(ctx -> ctx.next());
    router.get("/x").handler(this::a);"""
    assert _summary(analyze_files(_main(body))) == [(10, "GET", "/x", "a", None)]


def test_middleware_of_another_router_or_method_does_not_apply(analyze_files) -> None:
    src = IMPORTS + """public class Main extends AbstractVerticle {
  public void start() {
    Router router = Router.router(vertx);
    Router admin = Router.router(vertx);
    admin.route().handler(LoggerHandler.create());
    router.get("/x").handler(this::a);
    routes(router);
  }
  void routes(Router router) {
    router.get("/y").handler(this::b);
  }
  void setup(Router router) {
    router.route().handler(BodyHandler.create());
  }
  void a(Object c) {}
  void b(Object c) {}
}
"""
    assert [(r.endpoint, r.guards) for r in _routes(analyze_files({SRC: src}))] == [
        ("/x", None),
        ("/y", None),
    ]


def test_middleware_reaches_a_route_in_a_nested_block_but_not_out_of_one(analyze_files) -> None:
    body = """    Router router = Router.router(vertx);
    router.route().handler(LoggerHandler.create());
    if (config().getBoolean("admin")) {
      router.route().handler(BodyHandler.create());
      router.get("/admin").handler(this::a);
    }
    router.get("/x").handler(this::b);"""
    assert _summary(analyze_files(_main(body))) == [
        (12, "GET", "/admin", "a", ["LoggerHandler", "BodyHandler"]),
        (14, "GET", "/x", "b", ["LoggerHandler"]),
    ]


def test_a_failure_handler_alone_on_a_path_has_no_record(analyze_files) -> None:
    body = """    Router router = Router.router(vertx);
    router.get("/x").failureHandler(this::a);"""
    assert _routes(analyze_files(_main(body))) == []


def test_library_through_a_wildcard_import(analyze_files) -> None:
    imports = """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
import io.vertx.ext.web.handler.*;
"""
    body = """    Router router = Router.router(vertx);
    router.post("/x").handler(CorsHandler.create("*")).handler(this::a);"""
    assert _summary(analyze_files(_main(body, imports=imports))) == [
        (8, "POST", "/x", "a", ["CorsHandler"])
    ]


def test_a_same_package_class_hides_the_wildcard_library_name(analyze_files) -> None:
    # Java resolves `SessionHandler` to a.SessionHandler before the on-demand import
    imports = """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
import io.vertx.ext.web.handler.*;
"""
    body = """    Router router = Router.router(vertx);
    router.post("/x").handler(SessionHandler.create(null)).handler(this::a);"""
    files = _main(body, "SessionHandler", imports=imports)
    # a.SessionHandler is the app's: the first step, whose `handle` the record leads to
    st = analyze_files(files)
    assert _summary(st) == [(8, "POST", "/x", "handle", None)]
    assert [s.name for s in _routes(st)[0].handlerChain] == ["SessionHandler", "a"]


def test_an_app_class_imported_by_name_is_not_library(analyze_files) -> None:
    imports = """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
import io.vertx.ext.web.handler.*;
import a.web.BodyHandler;
"""
    body = """    Router router = Router.router(vertx);
    router.post("/x").handler(BodyHandler.create()).handler(this::a);"""
    # a.web.BodyHandler is the app's but not in the repository: a first step known by name only
    st = analyze_files(_main(body, imports=imports))
    assert _summary(st) == [(9, "POST", "/x", None, None)]
    assert [(s.name, s.handler) for s in _routes(st)[0].handlerChain] == [
        ("BodyHandler", None), ("a", "a")]


def test_vertx2_route_matcher_is_unchanged(analyze_files) -> None:
    src = """package a;
import org.vertx.java.core.http.RouteMatcher;
import org.vertx.java.platform.Verticle;
public class Main extends Verticle {
  public void start() {
    RouteMatcher rm = new RouteMatcher();
    rm.get("/x", this::a);
    rm.get("/x", this::b);
  }
  void a(Object c) {}
  void b(Object c) {}
}
"""
    st = analyze_files({SRC: src})
    assert [(r.handler, r.guards) for r in _routes(st)] == [("a", None), ("b", None)]
