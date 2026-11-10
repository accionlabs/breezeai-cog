"""Vert.x role table: docs/entry-point-roles/vertx.md — one test per row."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point


EP = {"route", "eventbus_consumer", "timer"}
SRC = "src/main/java/a/Api.java"

IMPORTS = """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
import io.vertx.serviceproxy.ServiceBinder;
import io.vertx.serviceproxy.ServiceProxyBuilder;
"""


def _verticle(start_body: str) -> dict[str, str]:
    """A verticle whose ``start()`` holds ``start_body``; line 8 is the body's first line."""
    return {
        SRC: IMPORTS
        + f"""public class Api extends AbstractVerticle {{
  public void start() {{
{start_body}
  }}
  void handle(Object c) {{}}
}}
"""
    }


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _at(stmts, line: int):
    return [s for s in stmts if s.path == SRC and s.startLine == line]


def test_router_verb_is_binding(analyze_files) -> None:
    st = analyze_files(
        _verticle("""    Router router = Router.router(vertx);
    router.get("/orders/:id").handler(this::handle);""")
    )
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.framework) == (
        "route",
        "GET",
        "route",
        "/orders/:id",
        "vertx",
    )


def test_router_verb_records_named_handler(analyze_files) -> None:
    st = analyze_files(
        _verticle("""    Router router = Router.router(vertx);
    router.get("/orders").handler(this::handle);""")
    )
    [r] = _eps(st)
    assert (r.handler, r.handlerLine) == ("handle", 11)


def test_router_verb_records_anonymous_handler(analyze_files) -> None:
    st = analyze_files(
        _verticle("""    Router router = Router.router(vertx);
    router.post("/orders").handler(ctx -> ctx.response().end());""")
    )
    [r] = _eps(st)
    assert (r.handler, r.handlerLine) == (None, 9)


def test_router_route_with_path_is_binding_for_every_verb(analyze_files) -> None:
    st = analyze_files(
        _verticle("""    Router router = Router.router(vertx);
    router.route("/any").handler(this::handle);""")
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint) == (None, "/any")


def test_groovy_route_matcher_is_binding(analyze_files) -> None:
    st = analyze_files(
        {
            "src/main/groovy/a/Routes.groovy": """package a
import org.vertx.groovy.core.http.RouteMatcher
def rm = new RouteMatcher()
rm.get("/users/:id") { req -> req.response.end() }
rm.with { post("/users", this.&create) }
"""
        }
    )
    got = {(r.method, r.endpoint) for r in _eps(st)}
    assert got == {("GET", "/users/:id"), ("POST", "/users")}


def test_router_route_without_path_has_no_record(analyze_files) -> None:
    st = analyze_files(
        _verticle("""    Router router = Router.router(vertx);
    router.route().handler(this::handle);""")
    )
    assert _eps(st) == []


def test_sub_router_mount_is_wiring(analyze_files) -> None:
    st = analyze_files(
        _verticle("""    Router router = Router.router(vertx);
    Router apiRouter = Router.router(vertx);
    apiRouter.get("/items").handler(this::handle);
    router.route("/api/*").subRouter(apiRouter);""")
    )
    assert _at(_eps(st), 11) == []
    [r] = _eps(st)
    assert r.endpoint == "/api/items"


def test_event_bus_consumer_is_binding(analyze_files) -> None:
    st = analyze_files(
        _verticle("""    vertx.eventBus().consumer("orders.created", this::handle);""")
    )
    [r] = _eps(st)
    assert (r.semanticType, r.endpoint, r.framework) == (
        "eventbus_consumer",
        "orders.created",
        "vertx",
    )


def test_event_bus_consumer_records_handler(analyze_files) -> None:
    st = analyze_files(
        _verticle("""    vertx.eventBus().consumer("orders.created", this::handle);""")
    )
    [r] = _eps(st)
    assert (r.handler, r.handlerLine) == ("handle", 10)


def test_address_forwarding_helper_is_wiring(analyze_files) -> None:
    st = analyze_files(
        {
            SRC: IMPORTS
            + """public class Api extends AbstractVerticle {
  public void start() {
    register("svc.a", this::handle);
    register("svc.b", this::handle);
  }
  private void register(String address, Object h) {
    vertx.eventBus().consumer(address, h);
  }
  void handle(Object c) {}
}
"""
        }
    )
    got = sorted((r.semanticType, r.endpoint) for r in _eps(st))
    assert got == [("eventbus_consumer", "svc.a"), ("eventbus_consumer", "svc.b")]


def test_timer_is_binding(analyze_files) -> None:
    st = analyze_files(_verticle("""    vertx.setPeriodic(5000, this::handle);"""))
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.endpoint, r.handler) == (
        "timer",
        "PERIODIC",
        "5000",
        "handle",
    )


def test_one_shot_timer_with_inline_handler(analyze_files) -> None:
    # A delay held in a variable is unknown: empty, never the variable name. The inline lambda is
    # an anonymous handler: no name, its line.
    st = analyze_files(_verticle("""    vertx.setTimer(delayMs, id -> {
      System.out.println("x");
    });"""))
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.endpoint, r.handler, r.handlerLine) == (
        "timer", "TIMER", None, None, 8,
    )


def test_service_binder_is_binding(analyze_files) -> None:
    st = analyze_files(
        _verticle(
            """    new ServiceBinder(vertx).setAddress("svc.orders").register(Object.class, this);"""
        )
    )
    [r] = _eps(st)
    assert (r.semanticType, r.endpoint) == ("eventbus_consumer", "svc.orders")


def test_service_proxy_builder_is_outbound(analyze_files) -> None:
    st = analyze_files(
        _verticle(
            """    Object p = new ServiceProxyBuilder(vertx).setAddress("svc.orders").build(Object.class);"""
        )
    )
    assert _eps(st) == []
    [s] = [s for s in _at(st, 8) if s.semanticType]
    assert (s.semanticType, s.endpoint) == ("eventbus_send", "svc.orders")



def test_service_builder_in_a_variable_and_unknown_receivers(analyze_files) -> None:
    st = analyze_files(_verticle("""    ServiceBinder binder = new ServiceBinder(vertx);
    binder.setAddress(System.getenv("ADDR")).register(Object.class, this);
    other.setAddress("not.a.proxy");"""))
    # The side comes from the variable's declared type; an unresolved address is empty (Q15); a
    # setAddress on an unknown receiver is not known to be a service proxy: no record.
    assert [(s.semanticType, s.endpoint) for s in st if s.semanticType] == [
        ("eventbus_consumer", None),
    ]

def test_proxygen_interface_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            "src/main/java/a/OrderService.java": """package a;
import io.vertx.codegen.annotations.ProxyGen;
@ProxyGen
public interface OrderService { void get(String id); }
"""
        }
    )
    assert [s for s in st if s.semanticType] == []


def test_send_and_publish_are_outbound(analyze_files) -> None:
    st = analyze_files(
        _verticle("""    vertx.eventBus().send("billing", "x");
    vertx.eventBus().request("pricing", "x");
    vertx.eventBus().publish("audit", "x");""")
    )
    assert _eps(st) == []
    got = [(s.semanticType, s.endpoint) for s in st if s.semanticType]
    assert got == [
        ("eventbus_send", "billing"),
        ("eventbus_send", "pricing"),
        ("eventbus_publish", "audit"),
    ]


def test_deploy_verticle_is_none(analyze_files) -> None:
    st = analyze_files(_verticle("""    vertx.deployVerticle("a.Other");"""))
    assert _eps(st) == []


def test_http_server_listen_is_none(analyze_files) -> None:
    st = analyze_files(_verticle("""    vertx.createHttpServer().listen(8080);"""))
    assert _eps(st) == []


def test_service_binder_is_one_binding_per_service_method(analyze_files) -> None:
    files = _verticle(
        """    new ServiceBinder(vertx).setAddress("svc.orders").register(OrderService.class, this);"""
    )
    files["src/main/java/a/OrderService.java"] = """package a;
import io.vertx.codegen.annotations.ProxyGen;
import io.vertx.codegen.annotations.ProxyIgnore;
@ProxyGen
public interface OrderService {
  void listOrders(Object h);
  void getOrder(String id, Object h);
  void getOrder(String id, String lang, Object h);
  @ProxyIgnore void internal();
  static OrderService create() { return null; }
}
"""
    st = analyze_files(files)
    # Every call reaches the one address; the message's action header picks the method.
    got = [(r.semanticType, r.endpoint, r.handler) for r in _eps(st)]
    assert got == [
        ("eventbus_consumer", "svc.orders", "listOrders"),
        ("eventbus_consumer", "svc.orders", "getOrder"),
    ]


def test_service_method_leads_to_the_implementing_class(analyze_files) -> None:
    # One consumer per interface method; the code that runs is the implementation's method.
    files = _verticle(
        "    OrderService svc = new OrderServiceImpl();\n"
        '    new ServiceBinder(vertx).setAddress("svc.orders").register(OrderService.class, svc);'
    )
    files["src/main/java/a/OrderService.java"] = (
        "package a;\nimport io.vertx.codegen.annotations.ProxyGen;\n"
        "@ProxyGen\npublic interface OrderService {\n  void listOrders(Object h);\n}\n")
    files["src/main/java/a/OrderServiceImpl.java"] = (
        "package a;\npublic class OrderServiceImpl implements OrderService {\n"
        "  public void listOrders(Object h) {}\n}\n")
    [r] = _eps(analyze_files(files))
    assert (r.handler, r.handlerPath, r.handlerLine) == (
        "listOrders", "src/main/java/a/OrderServiceImpl.java", 3)


def _binder(imports: str, register: str) -> dict[str, str]:
    """A verticle in package ``a`` with extra ``imports`` whose ``start()`` runs ``register``."""
    return {SRC: IMPORTS + imports + f"""public class Api extends AbstractVerticle {{
  public void start() {{
    {register}
  }}
}}
"""}


def _service(path: str, package: str, body: str) -> dict[str, str]:
    return {path: f"package {package};\nimport io.vertx.codegen.annotations.ProxyGen;\n{body}\n"}


_ORDERS = "@ProxyGen\npublic interface OrderService { void listOrders(); void getOrder(String id); }"


def test_service_interface_through_a_wildcard_import_is_one_binding_per_method(
        analyze_files) -> None:
    files = _binder("import com.acme.service.*;\n",
                    'new ServiceBinder(vertx).setAddress("order.service")'
                    ".register(OrderService.class, null);")
    files |= _service("src/main/java/com/acme/service/OrderService.java", "com.acme.service",
                      _ORDERS)
    got = [(r.endpoint, r.handler) for r in _eps(analyze_files(files))]
    assert got == [("order.service", "listOrders"), ("order.service", "getOrder")]


def test_nested_service_interface_is_one_binding_per_method(analyze_files) -> None:
    files = _binder("import com.acme.service.*;\n",
                    'new ServiceBinder(vertx).setAddress("billing.service")'
                    ".register(Api2.Billing.class, null);")
    files |= _service("src/main/java/com/acme/service/Api2.java", "com.acme.service",
                      "public class Api2 { @ProxyGen public interface Billing { void charge(); } }")
    got = [(r.endpoint, r.handler) for r in _eps(analyze_files(files))]
    assert got == [("billing.service", "charge")]


def test_service_interface_in_two_wildcard_packages_has_no_handler(analyze_files) -> None:
    files = _binder("import com.acme.one.*;\nimport com.acme.two.*;\n",
                    'new ServiceBinder(vertx).setAddress("order.service")'
                    ".register(OrderService.class, null);")
    files |= _service("src/main/java/com/acme/one/OrderService.java", "com.acme.one", _ORDERS)
    files |= _service("src/main/java/com/acme/two/OrderService.java", "com.acme.two",
                      "@ProxyGen\npublic interface OrderService { void other(); }")
    # Which interface the name means is unsure: the address only, no handler.
    got = [(r.endpoint, r.handler) for r in _eps(analyze_files(files))]
    assert got == [("order.service", None)]


def test_wildcard_service_shadowed_by_a_same_package_type_has_no_handler(analyze_files) -> None:
    files = _binder("import com.acme.service.*;\n",
                    'new ServiceBinder(vertx).setAddress("order.service")'
                    ".register(OrderService.class, null);")
    files |= _service("src/main/java/com/acme/service/OrderService.java", "com.acme.service",
                      _ORDERS)
    # Java resolves `OrderService` to package a's own type first, which is no service interface.
    files["src/main/java/a/OrderService.java"] = "package a;\npublic class OrderService {}\n"
    got = [(r.endpoint, r.handler) for r in _eps(analyze_files(files))]
    assert got == [("order.service", None)]


def test_router_variable_known_by_its_type_is_binding(analyze_files) -> None:
    files = _verticle(
        "    Router api = Router.router(vertx);\n"
        '    api.get("/orders").handler(this::handle);\n'
        "    io.vertx.ext.web.client.WebClient webClient = null;\n"
        '    webClient.get("/remote").send(ar -> {});'
    )
    got = [(r.semanticType, r.method, r.endpoint, r.handler) for r in _eps(analyze_files(files))]
    # `api` is a Router by its declaration; a WebClient `get` is an outbound call, never a route.
    assert got == [("route", "GET", "/orders", "handle")]


_IMPORTS = "package a;\nimport io.vertx.core.*;\nimport io.vertx.ext.web.Router;\n"
_USER_API = (_IMPORTS + "public class UserApi {\n"
             "  public static Router router(Vertx vertx) {\n"
             "    Router userRouter = Router.router(vertx);\n"
             '    userRouter.get("/users").handler(ctx -> ctx.end());\n'
             "    return userRouter;\n  }\n}\n")


def _routes(st) -> dict[str, str | None]:
    return {s.path.rsplit("/", 1)[-1]: s.endpoint for s in st if s.semanticType == "route"}


@pytest.mark.parametrize(("main", "expected"), [
    # mounted under /api from another file: the factory's routes are served there
    ('    router.mountSubRouter("/api", UserApi.router(vertx));\n', "/api/users"),
    ('    router.route("/v2/*").subRouter(UserApi.router(vertx));\n', "/v2/users"),
    # used as the main router: served at the root
    ("    vertx.createHttpServer().requestHandler(UserApi.router(vertx));\n", "/users"),
    # mounted at a prefix that does not resolve, or twice: where is unknown
    ("    router.mountSubRouter(base(), UserApi.router(vertx));\n", None),
    ('    router.mountSubRouter("/a", UserApi.router(vertx));\n'
     '    router.mountSubRouter("/b", UserApi.router(vertx));\n', None),
])
def test_router_factory_is_served_where_it_is_mounted(analyze_files, main, expected) -> None:
    st = analyze_files({
        "src/main/java/a/UserApi.java": _USER_API,
        "src/main/java/a/Main.java": (_IMPORTS + "public class Main extends AbstractVerticle {\n"
                                      "  public void start() {\n"
                                      "    Router router = Router.router(vertx);\n"
                                      + main + "  }\n}\n"),
    })
    assert _routes(st)["UserApi.java"] == expected


def test_field_sub_router_and_twice_mounted_sub_router(analyze_files) -> None:
    st = analyze_files(_verticle("""    Router router = Router.router(vertx);
    this.adminRouter = Router.router(vertx);
    adminRouter.get("/stats").handler(this::handle);
    router.route("/admin/*").subRouter(this.adminRouter);
    Router sharedRouter = Router.router(vertx);
    sharedRouter.get("/ping").handler(this::handle);
    router.mountSubRouter("/v1", sharedRouter);
    router.mountSubRouter("/v2", sharedRouter);"""))
    got = sorted((s.endpoint or "") for s in _eps(st))
    # the field's routes get its prefix; the mount itself is no route; shared is served twice
    assert got == ["", "/admin/stats"]


def test_handler_class_instance_is_its_handle_method_in_another_file(analyze_files) -> None:
    # Vert.x 2.x: `registerHandler(addr, new LoadHandler(eb))` — the class implements `Handler`,
    # whose one method `handle` is the code that runs; it lives in its own file.
    st = analyze_files({
        "src/main/java/p3/Main.java": """package p3;
import org.vertx.java.platform.Verticle;
public class Main extends Verticle {
  public void start() {
    vertx.eventBus().registerHandler("p3.wizard.load", new LoadHandler(vertx.eventBus()));
  }
}
""",
        "src/main/java/p3/LoadHandler.java": """package p3;
import org.vertx.java.core.Handler;
import org.vertx.java.core.eventbus.EventBus;
import org.vertx.java.core.eventbus.Message;
public class LoadHandler implements Handler<Message<Object>> {
  private final EventBus eb;
  public LoadHandler(EventBus eb) { this.eb = eb; }
  public void handle(Message<Object> msg) { }
}
""",
    })
    [c] = _eps(st)
    assert (c.semanticType, c.endpoint, c.handler, c.handlerLine, c.handlerPath) == (
        "eventbus_consumer", "p3.wizard.load", "handle", 8, "src/main/java/p3/LoadHandler.java",
    )


@pytest.mark.parametrize("mount", [
    '    router.mountSubRouter("/api", api);',
    '    router.route("/api/*").subRouter(api);',
])
def test_route_on_a_mounted_sub_router_has_the_mount_prefix(analyze_files, mount) -> None:
    st = analyze_files(_verticle(
        "    Router router = Router.router(vertx);\n"
        "    Router api = Router.router(vertx);\n"
        '    router.get("/health").handler(this::handle);\n'
        '    api.get("/items").handler(this::handle);\n' + mount))
    got = [(r.method, r.endpoint, r.handler) for r in _eps(st)]
    assert got == [("GET", "/health", "handle"), ("GET", "/api/items", "handle")]


_SPRING = "src/main/java/a/Jobs.java"


def test_vertx_timer_and_consumer_in_a_spring_component_are_captured(analyze_files) -> None:
    st = analyze_files({_SPRING: """package a;
import io.vertx.core.Vertx;
import org.springframework.stereotype.Component;
@Component
public class Jobs {
  void start(Vertx vertx) { vertx.setPeriodic(5000, id -> refresh()); }
  void bus(Vertx vertx) { vertx.eventBus().consumer("jobs.done", m -> refresh()); }
  void refresh() {}
}
"""})
    got = [(r.semanticType, r.method, r.endpoint, r.framework) for r in _eps(st)]
    assert got == [("timer", "PERIODIC", "5000", "vertx"),
                   ("eventbus_consumer", None, "jobs.done", "vertx")]


def test_vertx_route_in_a_spring_controller_is_captured_beside_its_spring_route(
        analyze_files) -> None:
    st = analyze_files({_SPRING: """package a;
import io.vertx.core.Vertx;
import io.vertx.ext.web.Router;
import org.springframework.web.bind.annotation.*;
@RestController
public class Jobs {
  @GetMapping("/spring") public String s() { return ""; }
  void routes(Vertx vertx) { Router r = Router.router(vertx); r.get("/vx").handler(c -> {}); }
}
"""})
    got = sorted((r.endpoint, r.framework) for r in _eps(st))
    assert got == [("/spring", "spring"), ("/vx", "vertx")]


def test_spring_file_without_vertx_gets_no_vertx_record(analyze_files) -> None:
    st = analyze_files({_SPRING: """package a;
import org.springframework.stereotype.Component;
@Component
public class Jobs {
  void start(Object scheduler) { scheduler.setPeriodic(5000, null); }
}
"""})
    assert [s for s in st if s.framework == "vertx"] == []


_LEGACY = "src/main/java/a/Legacy.java"


def test_router_by_type_only_needs_a_slash_path(analyze_files) -> None:
    st = analyze_files({_LEGACY: """package a;
import java.util.Map;
import org.vertx.java.core.http.RouteMatcher;
import org.vertx.java.platform.Verticle;
public class Legacy extends Verticle {
  static final String KEY = "customerId";
  public void start() {
    RouteMatcher r = new RouteMatcher();
    r.get("/ok", h);
    RouteMatcher routes = new RouteMatcher();
    routes.get("/docdef/:id", h);
  }
  String lookup(Map<String, String> r) {
    r.get(KEY);
    return r.get("customerId");
  }
}
"""})
    # `r` is a RouteMatcher in start() and a Map in lookup(): a lookup key is no route.
    got = [(r.method, r.endpoint) for r in st if r.semanticType == "route"]
    assert got == [("GET", "/ok"), ("GET", "/docdef/:id")]


def test_router_declared_only_in_a_comment_is_not_a_router(analyze_files) -> None:
    st = analyze_files({_LEGACY: """package a;
import java.util.Map;
import org.vertx.java.platform.Verticle;
public class Legacy extends Verticle {
  // RouteMatcher c = new RouteMatcher();
  /** Example: {@code RouteMatcher d = new RouteMatcher();} */
  String other(Map<String, String> c, Map<String, String> d) {
    String home = "http://example.org";
    return c.get("/x") + d.get("/y");
  }
}
"""})
    assert [s for s in st if s.semanticType == "route"] == []


def test_name_matched_router_keeps_routes_without_a_slash(analyze_files) -> None:
    st = analyze_files(_verticle(
        "    Router router = Router.router(vertx);\n"
        '    router.get("health").handler(this::handle);'))
    # Matched by its name, as before: the path rule only applies to routers known by type.
    assert [(r.method, r.endpoint) for r in _eps(st)] == [("GET", "health")]


def test_groovy_router_by_type_only_needs_a_slash_path(analyze_files) -> None:
    st = analyze_files({"src/main/groovy/a/Http.groovy": """package a
import org.vertx.groovy.core.http.RouteMatcher
def m = new RouteMatcher()
m.get('/ok') { req -> }
def lookup(Map m) { m.get('customerId') }
"""})
    assert [(r.method, r.endpoint) for r in st if r.semanticType == "route"] == [("GET", "/ok")]


def test_vertx_records_in_a_fixture_file_are_skipped(analyze_files) -> None:
    body = (IMPORTS + "public class Api extends AbstractVerticle {\n  public void start() {\n"
            '    Router router = Router.router(vertx);\n    router.get("/x").handler(c -> {});\n'
            '    vertx.eventBus().consumer("a.b", m -> {});\n  }\n}\n')
    st = analyze_files({"src/test/java/fixtures/a/Api.java": body})
    # A fixture is parsed for structure, never as a source of entry points.
    assert _eps(st) == []
