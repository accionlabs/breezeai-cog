"""Vert.x-web handler chains: the registrations of one router, method and path are one route
record; the handlers that run before the last one (and earlier path-less middleware) are its
``guards``. Vert.x 2 ``RouteMatcher`` calls are never grouped."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

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


def _main(body: str, *handlers: str) -> dict[str, str]:
    """A verticle whose ``start()`` holds ``body``; line 8 is the body's first line."""
    files = {
        SRC: IMPORTS
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


def test_the_ticket_example(analyze_files) -> None:
    st = analyze_files(
        _main(
            """    Router router = Router.router(vertx);
    String p = config().getString("prefix");
    router.get(p + "/request").handler(Session.create(o));
    router.get(p + "/request").handler(Page.create(o));
    router.get(p + "/request").failureHandler(Failure.create(o));
    router.post().handler(BodyHandler.create());
    router.post(p + "/return").handler(Session.create(o));
    router.post(p + "/return").handler(Validate.create(o));
    router.post(p + "/return").handler(Clean.create(o));
    router.post(p + "/return").handler(Success.create(o));
    router.post(p + "/return").failureHandler(Failure.create(o));
    router.post(p + "/back").handler(Session.create(o));
    router.post(p + "/back").handler(Back.create(o));""",
            "Session",
            "Page",
            "Failure",
            "Validate",
            "Clean",
            "Success",
            "Back",
        )
    )
    routes = _routes(st)
    assert [(r.method, r.startLine, r.endLine) for r in routes] == [
        ("GET", 10, 11),
        ("POST", 14, 17),
        ("POST", 19, 20),
    ]
    get, ret, back = routes
    assert (get.handler, get.handlerPath, get.guards) == (
        "handle",
        "src/main/java/a/Page.java",
        ["Session"],
    )
    assert (ret.handler, ret.handlerPath) == ("handle", "src/main/java/a/Success.java")
    assert ret.guards == ["BodyHandler", "Session", "Validate", "Clean"]
    assert back.guards == ["BodyHandler", "Session"]
    assert all(is_entry_point(r) for r in routes)


def test_one_registration_is_unchanged(analyze_files) -> None:
    st = analyze_files(
        _main("""    Router router = Router.router(vertx);
    router.get("/orders").handler(this::a);""")
    )
    [r] = _routes(st)
    assert (r.endpoint, r.handler, r.handlerLine, r.startLine, r.endLine, r.guards) == (
        "/orders",
        "a",
        11,
        9,
        9,
        None,
    )


def test_fluent_chain_on_one_statement(analyze_files) -> None:
    st = analyze_files(
        _main("""    Router router = Router.router(vertx);
    router.post("/orders").handler(BodyHandler.create()).handler(this::a).handler(this::b);""")
    )
    [r] = _routes(st)
    assert (r.handler, r.guards, r.startLine, r.endLine) == ("b", ["BodyHandler", "a"], 9, 9)


def test_fluent_and_separate_statements_give_the_same_record(analyze_files) -> None:
    fluent = _routes(
        analyze_files(
            _main("""    Router router = Router.router(vertx);
    router.post("/x").handler(this::a).handler(this::b);
    router.post("/x").handler(this::c);""")
        )
    )
    separate = _routes(
        analyze_files(
            _main("""    Router router = Router.router(vertx);
    router.post("/x").handler(this::a);
    router.post("/x").handler(this::b);
    router.post("/x").handler(this::c);""")
        )
    )
    assert [(r.handler, r.guards) for r in fluent] == [("c", ["a", "b"])]
    assert [(r.handler, r.guards) for r in separate] == [("c", ["a", "b"])]
    assert (separate[0].startLine, separate[0].endLine) == (9, 11)


def test_router_route_counts_only_when_registered_before(analyze_files) -> None:
    st = analyze_files(
        _main("""    Router router = Router.router(vertx);
    router.get("/early").handler(this::a);
    router.route().handler(LoggerHandler.create());
    router.get("/late").handler(this::b);""")
    )
    early, late = _routes(st)
    assert (early.endpoint, early.guards) == ("/early", None)
    assert (late.endpoint, late.guards) == ("/late", ["LoggerHandler"])


def test_verb_middleware_applies_to_that_verb_only(analyze_files) -> None:
    st = analyze_files(
        _main("""    Router router = Router.router(vertx);
    router.post().handler(BodyHandler.create());
    router.get("/x").handler(this::a);
    router.post("/x").handler(this::b);
    router.route("/x").handler(this::c);""")
    )
    by_method = {r.method: r for r in _routes(st)}
    assert by_method["GET"].guards is None
    assert by_method["POST"].guards == ["BodyHandler"]
    assert by_method[None].guards is None  # route("/x") serves every verb, not only POST


def test_pathless_registration_has_no_record(analyze_files) -> None:
    st = analyze_files(
        _main("""    Router router = Router.router(vertx);
    router.route().handler(LoggerHandler.create());
    router.post().handler(BodyHandler.create());""")
    )
    assert _routes(st) == []


def test_failure_handler_alone_on_a_path(analyze_files) -> None:
    st = analyze_files(
        _main("""    Router router = Router.router(vertx);
    router.get("/x").failureHandler(this::a);""")
    )
    assert _routes(st) == []


def test_failure_handler_does_not_join_the_chain(analyze_files) -> None:
    st = analyze_files(
        _main("""    Router router = Router.router(vertx);
    router.get("/x").handler(this::a);
    router.get("/x").failureHandler(this::c);
    router.get("/x").handler(this::b);""")
    )
    [r] = _routes(st)
    assert (r.handler, r.guards, r.startLine, r.endLine) == ("b", ["a"], 9, 11)


def test_inline_handlers_are_left_out_of_guards(analyze_files) -> None:
    st = analyze_files(
        _main("""    Router router = Router.router(vertx);
    router.get("/x").handler(ctx -> ctx.next());
    router.get("/x").handler(this::a);""")
    )
    [r] = _routes(st)
    assert (r.handler, r.guards) == ("a", None)
    assert (r.startLine, r.endLine) == (9, 10)


def test_different_paths_methods_and_routers_stay_apart(analyze_files) -> None:
    st = analyze_files(
        _main("""    Router router = Router.router(vertx);
    Router admin = Router.router(vertx);
    router.get("/x").handler(this::a);
    router.post("/x").handler(this::b);
    router.get("/y").handler(this::c);
    admin.get("/x").handler(this::c);""")
    )
    assert [(r.method, r.endpoint, r.guards) for r in _routes(st)] == [
        ("GET", "/x", None),
        ("POST", "/x", None),
        ("GET", "/y", None),
        ("GET", "/x", None),
    ]


def test_registrations_in_different_methods_are_not_grouped(analyze_files) -> None:
    files = _main("""    Router router = Router.router(vertx);
    router.get("/x").handler(this::a);""")
    files[SRC] = files[SRC].replace(
        "  void a(Object c) {}",
        '  void more(Router router) {\n    router.get("/x").handler(this::b);\n  }\n'
        "  void a(Object c) {}",
    )
    st = analyze_files(files)
    assert [r.handler for r in _routes(st)] == ["a", "b"]


def test_vertx2_route_matcher_is_never_grouped(analyze_files) -> None:
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
