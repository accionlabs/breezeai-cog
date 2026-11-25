"""Vert.x-web handler chains (java_vertx/route_chains.py): registrations that are steps of one
endpoint are one route record, at the first registration, leading to the first step, with every
step in ``handlerChain`` — only when the code shows that for certain; otherwise each registration
keeps its own record, as before."""

from __future__ import annotations

SRC = "src/main/java/a/Main.java"
IMPORTS = """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
import io.vertx.ext.web.handler.BodyHandler;
import io.vertx.ext.web.handler.LoggerHandler;
"""
SESSION = "src/main/java/a/SessionHandler.java"
PAGE = "src/main/java/a/PageHandler.java"
FAILURE = "src/main/java/a/FailureHandler.java"


def _handler_class(name: str) -> str:
    return f"""package a;
import io.vertx.core.Handler;
import io.vertx.ext.web.RoutingContext;
public class {name} implements Handler<RoutingContext> {{
  public static {name} create(Object o) {{ return new {name}(); }}
  public void handle(RoutingContext ctx) {{ ctx.next(); }}
}}
"""


def _main(body: str) -> dict[str, str]:
    """A verticle whose ``start()`` holds ``body``; line 8 is the body's first line."""
    files = {
        SRC: IMPORTS + f"""public class Main extends AbstractVerticle {{
  public void start() {{
{body}
  }}
  void a(Object c) {{}}
  void b(Object c) {{}}
}}
"""
    }
    for name in ("SessionHandler", "PageHandler", "FailureHandler"):
        files[f"src/main/java/a/{name}.java"] = _handler_class(name)
    return files


def _routes(stmts):
    return sorted((s for s in stmts if s.path == SRC and s.semanticType == "route"),
                  key=lambda s: s.startLine)


def _steps(route):
    return [(s.name, s.handler, s.handlerLine, s.handlerPath, s.phase)
            for s in route.handlerChain or []]


def test_one_endpoint_registered_over_several_statements(analyze_files) -> None:
    st = analyze_files(_main("""    Router router = Router.router(vertx);
    String prefix = config().getString("prefix");
    router.post().handler(BodyHandler.create());
    router.get(prefix + "/request").handler(SessionHandler.create(null));
    router.get(prefix + "/request").handler(PageHandler.create(null));
    router.get(prefix + "/request").failureHandler(FailureHandler.create(null));"""))
    [route] = _routes(st)
    assert (route.startLine, route.endLine, route.method, route.endpoint) == (
        11, 11, "GET", "${prefix}/request")
    # the record leads to the first step; POST-only BodyHandler is not a guard of a GET
    assert (route.handler, route.handlerLine, route.handlerPath, route.guards) == (
        "handle", 6, SESSION, None)
    assert _steps(route) == [
        ("SessionHandler", "handle", 6, SESSION, None),
        ("PageHandler", "handle", 6, PAGE, None),
        ("FailureHandler", "handle", 6, FAILURE, "failure"),
    ]
    # the other registrations have no record of their own
    assert not any(s.semanticType for s in st if s.path == SRC and s.startLine in (12, 13))


def test_library_handlers_ahead_are_guards_and_after_a_step_are_steps(analyze_files) -> None:
    [route] = _routes(analyze_files(_main("""    Router router = Router.router(vertx);
    router.route().handler(LoggerHandler.create());
    router.post("/x").handler(BodyHandler.create()).handler(this::a);
    router.post("/x").handler(BodyHandler.create()).handler(this::b);""")))
    assert (route.startLine, route.handler, route.guards) == (10, "a", ["LoggerHandler", "BodyHandler"])
    assert [(s.name, s.handler, s.handlerPath) for s in route.handlerChain] == [
        ("a", "a", None), ("BodyHandler", None, None), ("b", "b", None)]


def test_inline_steps_have_their_line_only(analyze_files) -> None:
    [route] = _routes(analyze_files(_main("""    Router router = Router.router(vertx);
    router.get("/x").handler(ctx -> ctx.next());
    router.get("/x").handler(this::a);""")))
    assert (route.handler, route.handlerLine) == (None, 9)
    assert _steps(route) == [(None, None, 9, None, None), ("a", "a", 12, None, None)]


def test_one_registration_with_a_failure_handler_has_a_chain(analyze_files) -> None:
    [route] = _routes(analyze_files(_main("""    Router router = Router.router(vertx);
    router.get("/x").handler(this::a).failureHandler(this::b);""")))
    assert route.handler == "a"
    assert [(s.name, s.phase) for s in route.handlerChain] == [("a", None), ("b", "failure")]


def test_one_registration_with_one_step_is_unchanged(analyze_files) -> None:
    [route] = _routes(analyze_files(_main("""    Router router = Router.router(vertx);
    router.post("/x").handler(BodyHandler.create()).handler(this::a);""")))
    assert (route.handler, route.guards, route.handlerChain) == ("a", ["BodyHandler"], None)


def test_another_path_between_the_steps_keeps_the_chain(analyze_files) -> None:
    routes = _routes(analyze_files(_main("""    Router router = Router.router(vertx);
    router.get("/x").handler(this::a);
    router.get("/y").handler(this::b);
    router.get("/x").handler(this::b);""")))
    assert [(r.startLine, r.endpoint, r.handler) for r in routes] == [
        (9, "/x", "a"), (10, "/y", "b")]
    assert [s.handler for s in routes[0].handlerChain] == ["a", "b"]


def _separate(analyze_files, body: str) -> list[tuple[int, str | None, object]]:
    """Each registration's own record, as before: (line, handler, handlerChain)."""
    return [(r.startLine, r.handler, r.handlerChain) for r in _routes(analyze_files(_main(body)))]


def test_no_chain_when_order_or_presence_is_uncertain(analyze_files) -> None:
    head = "    Router router = Router.router(vertx);\n"
    two = [(9, "a", None), (10, "b", None)]
    # a path-less handler of the verb registered between the steps runs between them
    assert _separate(analyze_files, head + """    router.get("/x").handler(this::a);
    router.get().handler(LoggerHandler.create());
    router.get("/x").handler(this::b);""") == [(9, "a", None), (11, "b", None)]
    # a wildcard path between the steps may match the request
    assert _separate(analyze_files, head + """    router.get("/x").handler(this::a);
    router.get("/*").handler(this::b);
    router.get("/x").handler(this::b);""")[::2] == [(9, "a", None), (11, "b", None)]
    # `.order(n)` / `.last()`: registration order is not run order
    assert _separate(analyze_files, head + """    router.get("/x").handler(this::a);
    router.get("/x").handler(this::b).order(-1);""") == two
    # differing consumes: alternatives, not steps
    assert _separate(analyze_files, head + """    router.get("/x").consumes("text/plain").handler(this::a);
    router.get("/x").consumes("application/json").handler(this::b);""") == two
    # an all-verb registration of the same path
    assert _separate(analyze_files, head + """    router.get("/x").handler(this::a);
    router.route("/x").handler(this::b);""") == two
    # one registration inside an `if` the other is not in
    assert _separate(analyze_files, head + """    router.get("/x").handler(this::a);
    if (debug()) { router.get("/x").handler(this::b); }""") == two


def test_no_chain_under_a_sub_router(analyze_files) -> None:
    routes = _routes(analyze_files(_main("""    Router router = Router.router(vertx);
    Router api = Router.router(vertx);
    api.get("/x").handler(this::a);
    api.get("/x").handler(this::b);
    router.route("/api/*").subRouter(api);""")))
    assert [(r.startLine, r.endpoint, r.handler, r.handlerChain) for r in routes] == [
        (10, "/api/x", "a", None), (11, "/api/x", "b", None)]


def test_failure_handler_alone_stays_error_wiring(analyze_files) -> None:
    assert _routes(analyze_files(_main("""    Router router = Router.router(vertx);
    router.get("/x").failureHandler(this::a);"""))) == []


def test_no_chain_when_one_endpoint_is_written_two_ways(analyze_files) -> None:
    # both render ${config:p}, but only the same expression surely reads the same value
    assert _separate(analyze_files, """    Router router = Router.router(vertx);
    String p = config().getString("p");
    router.get(p).handler(this::a);
    router.get(config().getString("p")).handler(this::b);""") == [(10, "a", None), (11, "b", None)]
