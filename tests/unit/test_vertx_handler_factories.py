"""Vert.x handlers built by a static factory (parsers/java_vertx/handler_factories.py) —
docs/entry-point-roles/vertx.md."""

from __future__ import annotations

SRC = "src/main/java/com/acme/"
V3 = """import io.vertx.core.Handler;
import io.vertx.ext.web.RoutingContext;
"""


def _java(name: str, body: str, imports: str = V3, package: str = "com.acme") -> dict[str, str]:
    folder = f"src/main/java/{package.replace('.', '/')}/"
    return {f"{folder}{name}.java": f"package {package};\n{imports}{body}\n"}


def _handler(name: str, factory: str) -> dict[str, str]:
    return _java(name, f"""public class {name} implements Handler<RoutingContext> {{
  private final Object options;
  {name}(Object options) {{ this.options = options; }}
{factory}
  public void handle(RoutingContext ctx) {{ }}
}}""")


def _main(routes: str, imports: str = "") -> dict[str, str]:
    return _java("MainVerticle", f"""public class MainVerticle extends AbstractVerticle {{
  public void start() {{
    Router router = Router.router(vertx);
{routes}
  }}
}}""", "import io.vertx.core.AbstractVerticle;\nimport io.vertx.ext.web.Router;\n"
        "import io.vertx.ext.web.handler.BodyHandler;\n" + imports)


def _routes(stmts):
    return sorted(
        ((s.endpoint, s.handler, s.handlerLine, (s.handlerPath or "").rsplit("/", 1)[-1] or None)
         for s in stmts if s.semanticType == "route"),
        key=lambda r: tuple(str(x) for x in r))


FACTORY = """  public static Handler<RoutingContext> create(Object o) { return new %s(o); }"""


def test_handler_built_by_a_static_factory_is_that_class_handle(analyze_files) -> None:
    files = {
        **_handler("SessionHandler", FACTORY % "SessionHandler"),
        # Any name, and a factory building another class of the repo.
        **_handler("Impl", ""),
        **_java("Handlers", """public class Handlers {
  public static Handler<RoutingContext> session(Object o) { return new Impl(o); }
}"""),
        **_main("""    router.get("/a").handler(SessionHandler.create(config()));
    router.get("/b").handler(Handlers.session(config()));
    router.get("/c").handler(BodyHandler.create());"""),
    }
    # `handle` is on line 8 of each handler file; a library factory gives nothing.
    assert _routes(analyze_files(files)) == [
        ("/a", "handle", 8, "SessionHandler.java"),
        ("/b", "handle", 8, "Impl.java"),
        ("/c", None, None, None),
    ]


def test_factory_with_any_other_shape_is_not_followed(analyze_files) -> None:
    files = {
        # A cache in front of the construction.
        **_handler("Cached", """  static Cached one;
  public static Cached create(Object o) { if (one == null) one = new Cached(o); return one; }"""),
        # Not static: an instance method.
        **_handler("Instance", "  public Instance create(Object o) { return new Instance(o); }"),
        # An anonymous class.
        **_handler("Anon", """  public static Handler<RoutingContext> create(Object o) {
    return new Handler<RoutingContext>() { public void handle(RoutingContext c) { } };
  }"""),
        # A same-named class of another package, imported: its factory is not this one.
        **_java("Other", """public class Other {
  public static Object create(Object o) { return null; }
}""", "", "com.other"),
        **_handler("Other", FACTORY % "Other"),
        **_main("""    router.get("/cached").handler(Cached.create(config()));
    router.get("/instance").handler(Instance.create(config()));
    router.get("/anon").handler(Anon.create(config()));
    router.get("/other").handler(Other.create(config()));""", "import com.other.Other;\n"),
    }
    assert _routes(analyze_files(files)) == [
        ("/anon", None, None, None),
        ("/cached", None, None, None),
        ("/instance", None, None, None),
        ("/other", None, None, None),
    ]


def test_consumer_handler_built_by_a_factory(analyze_files) -> None:
    files = {
        **_handler("OrderHandler", FACTORY % "OrderHandler"),
        **_java("Wiring", """public class Wiring extends AbstractVerticle {
  public void start() { vertx.eventBus().consumer("orders", OrderHandler.create(config())); }
}""", "import io.vertx.core.AbstractVerticle;\n"),
    }
    got = [(s.endpoint, s.handler, (s.handlerPath or "").rsplit("/", 1)[-1])
           for s in analyze_files(files) if s.semanticType == "eventbus_consumer"]
    assert got == [("orders", "handle", "OrderHandler.java")]
