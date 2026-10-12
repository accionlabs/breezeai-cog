"""Ktor role table: docs/entry-point-roles/ktor.md — one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}
SRC = "src/main/kotlin/a/App.kt"
HEAD = """package a
import io.ktor.server.application.*
import io.ktor.server.routing.*
import io.ktor.server.response.*
import io.ktor.server.auth.*
import io.ktor.server.websocket.*
"""


def _app(body: str) -> dict[str, str]:
    """``body`` goes inside ``fun Application.module()``; its first line is line 8."""
    return {SRC: HEAD + "fun Application.module() {\n" + body + "\n}\n"}


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def test_verb_in_routing_is_binding(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        get("/health") { call.respondText("ok") }
    }""")
    )
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.endpoint, r.framework) == (
        "route",
        "GET",
        "/health",
        "ktor",
    )


@pytest.mark.xfail(strict=True, reason="Ktor routes have no routeKind")
def test_verb_in_routing_has_route_kind(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        get("/health") { call.respondText("ok") }
    }""")
    )
    [r] = _eps(st)
    assert r.routeKind == "route"


@pytest.mark.xfail(strict=True, reason="anonymous Ktor handler has no handlerLine")
def test_verb_in_routing_records_anonymous_handler(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        get("/health") { call.respondText("ok") }
    }""")
    )
    [r] = _eps(st)
    assert (r.handler, r.handlerLine) == (None, 9)


def test_verb_without_path_takes_the_prefix(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        route("/items") {
            get { call.respondText("i") }
        }
    }""")
    )
    [r] = _eps(st)
    assert (r.method, r.endpoint) == ("GET", "/items")


def test_route_block_is_wiring(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        route("/api") {
            post("/orders") { call.respondText("c") }
        }
    }""")
    )
    [r] = _eps(st)  # the route("/api") block has no record of its own
    assert (r.method, r.endpoint) == ("POST", "/api/orders")


@pytest.mark.xfail(strict=True, reason="Ktor webSocket routes are not recorded")
def test_websocket_is_binding(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        webSocket("/ws") { }
    }""")
    )
    [r] = _eps(st)
    assert (r.semanticType, r.routeKind, r.endpoint) == ("route", "ws", "/ws")


def test_authenticate_block_has_no_record(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        authenticate("jwt") {
            get("/me") { call.respondText("me") }
        }
    }""")
    )
    [r] = _eps(st)
    assert r.endpoint == "/me"


@pytest.mark.xfail(strict=True, reason="authenticate(...) is not recorded in guards")
def test_authenticate_is_guard(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        authenticate("jwt") {
            get("/me") { call.respondText("me") }
        }
    }""")
    )
    [r] = _eps(st)
    assert r.guards and "jwt" in r.guards[0]


def test_install_is_none(analyze_files) -> None:
    st = analyze_files(_app("""    install(Authentication) { }"""))
    assert _eps(st) == []


def test_embedded_server_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            SRC: HEAD
            + """fun main() {
    embeddedServer(Netty, port = 8080, module = Application::module).start(wait = true)
}
"""
        }
    )
    assert _eps(st) == []


def test_http_client_call_is_not_entry_point(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        get("/proxy") { val r = client.get("http://billing/api/x"); call.respondText("p") }
    }""")
    )
    assert [r.endpoint for r in _eps(st)] == ["/proxy"]
