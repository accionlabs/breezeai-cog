"""Ktor role table: docs/entry-point-roles/ktor.md — one test per row."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.contract import is_entry_point

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
    return [s for s in stmts if is_entry_point(s)]


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


def test_verb_in_routing_has_route_kind(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        get("/health") { call.respondText("ok") }
    }""")
    )
    [r] = _eps(st)
    assert r.routeKind == "route"


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


def test_route_block_with_a_non_path_argument_makes_the_address_unknown(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        route("/api") {
            route(prefixFromEnv()) { get("/x") { call.respondText("x") } }
        }
    }""")
    )
    [r] = _eps(st)  # never the partial /api/x
    assert (r.method, r.endpoint) == ("GET", None)


def test_websocket_is_binding(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing {
        webSocket("/ws") { }
        route("/chat") {
            webSocket { }
        }
    }""")
    )
    assert sorted((r.semanticType, r.method, r.routeKind, r.endpoint) for r in _eps(st)) == [
        ("route", "WEBSOCKET", "ws", "/chat"),
        ("route", "WEBSOCKET", "ws", "/ws"),
    ]


def test_client_websocket_session_is_not_a_binding(analyze_files) -> None:
    st = analyze_files(
        _app("""    routing { }
    runBlocking {
        client.webSocket(host = "example.com", path = "/ws") { }
    }""")
    )
    assert _eps(st) == []


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


def _auth(analyze_files, open_block: str, nested: str = "") -> dict[str, bool | None]:
    inner = (f"            {nested} {{\n                get(\"/inner\") {{ call.respondText(\"i\") }}\n"
             "            }\n") if nested else ""
    st = analyze_files(_app(f"""    routing {{
        {open_block} {{
            get("/feed") {{ call.respondText("f") }}
{inner}        }}
    }}"""))
    return {r.endpoint: r.authRequired for r in _eps(st)}


def test_required_authenticate_asserts_auth(analyze_files) -> None:
    for block in ('authenticate("jwt")', "authenticate", 'authenticate("jwt", optional = false)',
                  'authenticate("jwt", strategy = AuthenticationStrategy.Required)'):
        assert _auth(analyze_files, block) == {"/feed": True}, block


def test_optional_or_unreadable_authenticate_leaves_auth_unknown(analyze_files) -> None:
    # Anonymous calls may get through: the guard is listed, authRequired stays empty.
    for block in ('authenticate("jwt", optional = true)', 'authenticate("jwt", optional = flag)',
                  'authenticate("jwt", strategy = AuthenticationStrategy.Optional)'):
        assert _auth(analyze_files, block) == {"/feed": None}, block


def test_required_block_inside_an_optional_one_asserts_auth(analyze_files) -> None:
    got = _auth(analyze_files, 'authenticate("jwt", optional = true)', 'authenticate("admin")')
    assert got == {"/feed": None, "/inner": True}


# ── Path constants from another file ────────────────────────────────────────────────────────
PATHS = "src/main/kotlin/com/acme/Paths.kt"
ROUTES = "src/main/kotlin/com/acme/Routes.kt"
ROUTING = "import io.ktor.server.routing.*\n"


def _routes(files: dict[str, str], analyze_files) -> list[tuple[str | None, str | None]]:
    return [(r.method, r.endpoint) for r in _eps(analyze_files(files)) if r.path == ROUTES]


def _kt(package: str, imports: str, body: str) -> str:
    return f"package {package}\n{imports}{body}\n"


def test_object_and_top_level_constants_from_another_file_resolve(analyze_files) -> None:
    files = {
        PATHS: _kt("com.acme", "", 'object Paths { const val USERS = "/users" }\n'
                                   'const val API = "/api"'),
        ROUTES: _kt("com.acme", ROUTING + "import com.acme.Paths\nimport com.acme.API\n",
                    """fun Route.users() {
    get(Paths.USERS) { }
    route(API) {
        get("/items") { }
    }
}"""),
    }
    assert _routes(files, analyze_files) == [("GET", "/users"), ("GET", "/api/items")]


def test_constant_of_the_same_package_resolves_without_an_import(analyze_files) -> None:
    files = {
        PATHS: _kt("com.acme", "", 'object Paths {\n  const val USERS = "/users"\n}\n'
                                   'class Holder {\n  companion object {\n'
                                   '    const val ADMIN = "/admin"\n  }\n}'),
        ROUTES: _kt("com.acme", ROUTING,
                    "fun Route.r() {\n    get(Paths.USERS) { }\n    get(Holder.ADMIN) { }\n}"),
    }
    assert _routes(files, analyze_files) == [("GET", "/users"), ("GET", "/admin")]


def test_imported_member_alias_and_interpolation_resolve(analyze_files) -> None:
    files = {
        PATHS: _kt("com.acme.paths", "", 'object Paths { const val USERS = "/users" }\n'
                                         'const val API = "/api"'),
        ROUTES: _kt("com.acme", ROUTING + "import com.acme.paths.Paths.USERS\n"
                    "import com.acme.paths.Paths as P\nimport com.acme.paths.API\n",
                    """fun Route.r() {
    get(USERS) { }
    get(P.USERS) { }
    get("$API/orders") { }
}"""),
    }
    assert _routes(files, analyze_files) == [
        ("GET", "/users"), ("GET", "/users"), ("GET", "/api/orders")]


def test_constant_with_two_values_across_files_leaves_the_endpoint_empty(analyze_files) -> None:
    files = {
        PATHS: _kt("com.acme", "", 'const val API = "/api"'),
        "src/other/kotlin/com/acme/Paths.kt": _kt("com.acme", "", 'const val API = "/v2"'),
        ROUTES: _kt("com.acme", ROUTING + "import com.acme.API\n",
                    'fun Route.r() {\n    get(API) { }\n}'),
    }
    assert _routes(files, analyze_files) == [("GET", None)]


def test_constant_not_visible_from_the_file_is_not_used(analyze_files) -> None:
    files = {
        "src/main/kotlin/com/other/Paths.kt": _kt(
            "com.other", "", 'object Paths { const val USERS = "/users" }\nconst val API = "/api"'),
        "src/main/kotlin/com/star/Star.kt": _kt("com.star", "", 'const val STAR = "/star"'),
        ROUTES: _kt("com.acme", ROUTING + "import com.star.*\n", """fun Route.r() {
    get(API) { }
    get(Paths.USERS) { }
    get(STAR) { }
}"""),
    }
    # Not imported, not in this package; a star import is not followed: unknown, never guessed.
    assert _routes(files, analyze_files) == [("GET", None), ("GET", None), ("GET", None)]


def test_only_a_resolvable_val_of_an_object_or_top_level_is_shared(analyze_files) -> None:
    files = {
        PATHS: _kt("com.acme", "import com.acme.base.BASE\n", """var MUTABLE = "/m"
const val BUILT = "$BASE/users"
class Instance {
  val inst = "/i"
}
fun f() { val local = "/l" }"""),
        "src/main/kotlin/com/acme/base/Base.kt": _kt("com.acme.base", "", 'const val BASE = "/b"'),
        ROUTES: _kt("com.acme", ROUTING, """fun Route.r() {
    get(MUTABLE) { }
    get(BUILT) { }
    get(Instance.inst) { }
    get(local) { }
}"""),
    }
    # A var can change; a constant built from another file's constant, an instance property
    # and a local are not shared: all unknown.
    assert _routes(files, analyze_files) == [("GET", None)] * 4


def test_a_constant_of_this_file_shadows_an_import(analyze_files) -> None:
    files = {
        PATHS: _kt("com.acme.paths", "", 'const val API = "/api"'),
        ROUTES: _kt("com.acme", ROUTING + "import com.acme.paths.API\n", """const val API = "/local"
fun Route.r() {
    get(API) { }
}"""),
    }
    assert _routes(files, analyze_files) == [("GET", "/local")]


@pytest.mark.parametrize("jobs", [1, 2])
def test_cross_file_constant_resolves_for_any_jobs(tmp_path, jobs) -> None:
    import gzip
    import json

    from breezeai_cog import analyze_repo

    repo = tmp_path / "repo"
    for rel, src in {
        PATHS: _kt("com.acme", "", 'object Paths { const val USERS = "/users" }'),
        ROUTES: _kt("com.acme", ROUTING + "import com.acme.Paths\n",
                    "fun Route.r() {\n    get(Paths.USERS) { }\n}"),
    }.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(src)
    result = analyze_repo(repo, capture_statements=True, out=tmp_path / "out", jobs=jobs)
    lines = gzip.open(result.out_path, "rt", encoding="utf-8").read().splitlines()
    rec = next(json.loads(line) for line in lines if f'"{ROUTES}"' in line)
    assert [s["endpoint"] for s in rec["statements"] if s.get("semanticType") == "route"] == [
        "/users"]
