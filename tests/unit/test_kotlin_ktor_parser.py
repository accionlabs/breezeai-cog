"""Tests for the KtorParser — Ktor (server) framework route detection."""

from __future__ import annotations

from breezeai_cog.core.registry import discover_builtin, select
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.kotlin.parser import KotlinParser
from breezeai_cog.parsers.kotlin_ktor.parser import KtorParser
from breezeai_cog.schemas import FileRecord

_PATH = "src/main/kotlin/com/acme/Application.kt"

_KTOR_SRC = b'''\
package com.acme

import io.ktor.server.routing.*
import io.ktor.server.application.*
import io.ktor.server.response.*

fun Application.configureRouting() {
    routing {
        get("/users") { call.respond(listOf<String>()) }
        post("/users") { call.respond(Unit) }
        put("/users/{id}") { call.respond(Unit) }
        delete("/users/{id}") { call.respond(Unit) }
        route("/api") {
            get("/items") { call.respond(listOf<String>()) }
            post("/items") { call.respond(Unit) }
        }
    }
}
'''

_PLAIN_SRC = b'''\
package com.acme

class Foo {
    fun bar() = 42
}
'''


def _parse(src: bytes, capture: bool = True) -> FileRecord:
    parser = KtorParser()
    ctx = ParseContext(
        path=_PATH,
        abs_path=None,  # type: ignore[arg-type]
        source=src,
        repo_root=None,  # type: ignore[arg-type]
        capture_statements=capture,
    )
    return parser.parse_file(ctx)


def test_ktor_claims():
    """KtorParser claims .kt files that import from io.ktor.server.*."""
    parser = KtorParser()
    assert parser.claims(_PATH, _KTOR_SRC) is True
    assert parser.claims(_PATH, _PLAIN_SRC) is False


def test_ktor_priority_over_base():
    """KtorParser has higher priority than KotlinParser."""
    assert KtorParser().priority > KotlinParser().priority


def test_ktor_get_route():
    """`get("/users") { }` produces a route Statement with correct fields."""
    rec = _parse(_KTOR_SRC)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    get_routes = [s for s in routes if s.method == "get"]
    assert any(s.endpoint == "/users" for s in get_routes), (
        f"Expected /users GET route; got: {[(s.method, s.endpoint) for s in routes]}"
    )


def test_ktor_multiple_verbs():
    """All HTTP verb calls produce separate route statements."""
    rec = _parse(_KTOR_SRC)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    methods = {s.method for s in routes}
    assert {"get", "post", "put", "delete"} <= methods


def test_ktor_nested_route_prefix():
    """`route("/api") { get("/items") }` produces endpoint="/api/items"."""
    rec = _parse(_KTOR_SRC)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert any(s.endpoint == "/api/items" for s in routes), (
        f"Expected /api/items route; got endpoints: {[s.endpoint for s in routes]}"
    )


def test_ktor_no_statements_without_flag():
    """`capture_statements=False` must yield no route statements."""
    rec = _parse(_KTOR_SRC, capture=False)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert routes == []


def test_ktor_base_kotlin_extraction_preserved():
    """KtorParser still extracts Kotlin classes and functions via the base parser."""
    rec = _parse(_KTOR_SRC)
    fn = next((f for f in rec.functions if f.name == "configureRouting"), None)
    assert fn is not None, "configureRouting function must be extracted"
    assert rec.language == "kotlin"


def test_ktor_registry_selection(tmp_path):
    """Registry selects KtorParser (not KotlinParser) for a Ktor .kt file."""
    discover_builtin()
    kt_file = tmp_path / "App.kt"
    parser = select(str(kt_file), _KTOR_SRC)
    assert parser is not None
    assert parser.name == "kotlin-ktor", f"Expected kotlin-ktor, got {parser.name}"


_CONST_ROUTE_SRC = b'''\
package com.acme

import io.ktor.server.routing.*
import io.ktor.server.application.*

const val API_PATH = "/graphql"
const val ASYNC_PATH = "/graphql/async"

fun Application.configureRouting() {
    routing {
        route(API_PATH) {
            post {
                // handles POST /graphql (inherited prefix, no suffix)
            }
            post(ASYNC_PATH) {
                // handles POST /graphql/async
            }
        }
    }
}
'''


def test_ktor_constant_route_prefix():
    """`route(CONSTANT)` uses `{CONSTANT}` as the prefix placeholder."""
    rec = _parse(_CONST_ROUTE_SRC)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    endpoints = [s.endpoint for s in routes]
    assert any("{API_PATH}" in ep for ep in endpoints), (
        f"Expected {{API_PATH}} placeholder in an endpoint; got {endpoints}"
    )


def test_ktor_no_arg_post_inherits_prefix():
    """`post {{ }}` inside `route(CONSTANT)` emits route with prefix as endpoint."""
    rec = _parse(_CONST_ROUTE_SRC)
    routes = [s for s in rec.statements if s.semanticType == "route" and s.method == "post"]
    # One route should be exactly "{API_PATH}" (no suffix), another "{API_PATH}{ASYNC_PATH}"
    endpoints = {s.endpoint for s in routes}
    assert "{API_PATH}" in endpoints, f"Expected plain-prefix POST route; got {endpoints}"


def test_ktor_framework_set_regardless_of_routes():
    """framework='ktor' is set even when no string-literal routes are captured."""
    rec = _parse(_CONST_ROUTE_SRC)
    assert rec.framework == "ktor"


_CLIENT_SRC = b'''\
package com.acme

import io.ktor.server.routing.*
import io.ktor.server.application.*

fun Application.configureRouting() {
    routing {
        get("/profile") {
            val info = httpClient.get("https://www.googleapis.com/oauth2/v2/userinfo")
        }
    }
}
'''


def test_qualified_call_not_a_route() -> None:
    """Qualified calls like httpClient.get(...) must not be captured as route statements."""
    rec = _parse(_CLIENT_SRC)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    outbound_url = "https://www.googleapis.com/oauth2/v2/userinfo"
    assert all(s.endpoint != outbound_url for s in routes), (
        f"httpClient.get(...) must not be a route; got: {[(s.method, s.endpoint) for s in routes]}"
    )
    assert any(s.endpoint == "/profile" for s in routes), (
        f"Expected /profile route; got: {[(s.method, s.endpoint) for s in routes]}"
    )
