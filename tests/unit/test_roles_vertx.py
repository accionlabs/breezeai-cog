"""Vert.x role table: docs/entry-point-roles/vertx.md — one test per row."""

from __future__ import annotations

import pytest

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
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


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


@pytest.mark.xfail(strict=True, reason="Vert.x timer has no method, endpoint or handler")
def test_timer_is_binding(analyze_files) -> None:
    st = analyze_files(_verticle("""    vertx.setPeriodic(5000, this::handle);"""))
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.endpoint, r.handler) == (
        "timer",
        "PERIODIC",
        "5000",
        "handle",
    )


@pytest.mark.xfail(
    strict=True, reason="ServiceBinder registration is typed service_proxy, not eventbus_consumer"
)
def test_service_binder_is_binding(analyze_files) -> None:
    st = analyze_files(
        _verticle(
            """    new ServiceBinder(vertx).setAddress("svc.orders").register(Object.class, this);"""
        )
    )
    [r] = _eps(st)
    assert (r.semanticType, r.endpoint) == ("eventbus_consumer", "svc.orders")


@pytest.mark.xfail(
    strict=True, reason="ServiceProxyBuilder client is typed service_proxy, not eventbus_send"
)
def test_service_proxy_builder_is_outbound(analyze_files) -> None:
    st = analyze_files(
        _verticle(
            """    Object p = new ServiceProxyBuilder(vertx).setAddress("svc.orders").build(Object.class);"""
        )
    )
    assert _eps(st) == []
    [s] = [s for s in _at(st, 8) if s.semanticType]
    assert (s.semanticType, s.endpoint) == ("eventbus_send", "svc.orders")


@pytest.mark.xfail(strict=True, reason="@ProxyGen interface gets a synthetic service_proxy record")
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
