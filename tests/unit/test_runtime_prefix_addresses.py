"""One notation for an address with a part read at runtime (``parsers.addresses``): the part is
marked ``${name}`` where it stands, for every framework and both directions. ``{name}`` stays a
path parameter (one segment); both name a value by its whole member path (``config.contextRoot``,
``user.id``), less ``this.`` / ``self.``, and a configuration entry or environment variable by its
kind and key (``config:a.b``, ``env:X``). An address of named parts only is kept (it names its
source); one whose parts have no name, and a router mounted at two places, stay empty; a resolved
prefix is unchanged."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.addresses import (
    inbound_template,
    outbound_template,
    runtime_name,
    value_path,
    informative,
)
from breezeai_cog.parsers.statements_common import url_placeholder


def _one(stmts, semantic: str, path: str, line: int | None = None):
    found = [
        s
        for s in stmts
        if s.semanticType == semantic and s.path == path and (line is None or s.startLine == line)
    ]
    assert len(found) == 1, found
    return found[0]


def _addr(stmt) -> str | None:
    return stmt.endpoint


# ── the helpers ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "expr, name",
    [
        ("config.contextRoot", "config.contextRoot"),
        ("config . contextRoot", "config.contextRoot"),
        ("this.prefix", "prefix"),
        ("self.settings.base", "settings.base"),
        ("$scope.base", "$scope.base"),
        ('config().getString("pathPrefix")', "config:pathPrefix"),
        ('container.config().getObject("api-verticle").getString("address")', "config:api-verticle.address"),
        ('config().getJsonObject("a").getString("b", "dflt")', "config:a.b"),
        ('env.getProperty("app.base")', "config:app.base"),
        ('config.get("base")', "config:base"),
        ("os.getenv('BASE')", "env:BASE"),
        ('os.environ.get("BASE", "/")', "env:BASE"),
        ('os.environ["BASE"]', "env:BASE"),
        ("process.env.BASE_PATH", "env:BASE_PATH"),
        ("process.env['BASE_PATH']", "env:BASE_PATH"),
        ('System.getenv("BUS")', "env:BUS"),
        ('Environment.GetEnvironmentVariable("HOST")', "env:HOST"),
        ('ENV["ROOT"]', "env:ROOT"),
        ("config:a.b", "config:a.b"),  # already named: unchanged
        ("config().getString(key)", "getString"),  # a computed key is no config entry
        ('map.get("k")', "k"),  # another lookup by a literal key: the key
        ("prefixFromEnv()", "prefixFromEnv"),
        ("a ? b : c", "value"),
        ("", "value"),
    ],
)
def test_runtime_name(expr, name) -> None:
    assert runtime_name(expr) == name


@pytest.mark.parametrize(
    "expr, path",
    [
        ("id", "id"),
        ("user.id", "user.id"),
        ("req.params.user.id", "req.params.user.id"),
        ("this.userId", "userId"),
        ("self.base_url", "base_url"),
        ("this", "this"),  # nothing after it: kept
        ("getId()", None),
        ("user.getId()", None),
        ('row["id"]', None),
        ("a + b", None),
    ],
)
def test_value_path(expr, path) -> None:
    assert value_path(expr) == path


@pytest.mark.parametrize(
    "expr, placeholder",
    [
        ("user.id", "{user.id}"),  # two values never share a name: {user.id} / {order.id}
        ("order.id", "{order.id}"),
        ("this.userId", "{userId}"),
        ("encodeURIComponent(id)", "{param}"),  # a call is not a name
        ("@user.id", "{id}"),  # not a plain path: its last part, as before
        ("$user->id", "{param}"),
    ],
)
def test_url_placeholder(expr, placeholder) -> None:
    assert url_placeholder(expr) == placeholder


@pytest.mark.parametrize(
    "rendered, url",
    [
        ("{contextRoot}/orders/confirm", "${contextRoot}/orders/confirm"),
        ("{base}/orders/{id}", "${base}/orders/{id}"),  # the base, then a request value
        ("{host}:{port}/x", "${host}:${port}/x"),
        ("{scheme}://{host}/x/{id}", "${scheme}://${host}/x/{id}"),
        ("https://{host}/x", "https://${host}/x"),
        ("/api/{id}", "/api/{id}"),  # no runtime part in the base: unchanged
        ("{base}", "${base}"),  # no literal part: still names where the URL comes from
        ("{base}{path}", "${base}${path}"),
        ("{base}/", "${base}/"),
        ("{value}", None),  # no name: says nothing
        ("{param}/{param}", None),
        ("/a/{config:v}", "/a/${config:v}"),  # a config entry is never a request value
        ("${ctx}/x", "${ctx}/x"),  # already marked
    ],
)
def test_outbound_template(rendered, url) -> None:
    assert outbound_template(rendered) == url


def test_inbound_template_and_with_literal() -> None:
    assert inbound_template("{pathPrefix}/request") == "${pathPrefix}/request"
    assert inbound_template("/api/{config.version}/orders/:id") == "/api/${config.version}/orders/:id"
    assert inbound_template("{a}/{b}") == "${a}/${b}"
    assert inbound_template("{value}") is None
    assert informative("${base}/x") == "${base}/x"
    assert informative("${base}/") == "${base}/"
    assert informative("${value}/{param}") is None
    assert informative("/") == "/"  # a literal address is never cleared


# ── outbound: axios / fetch (shared URL rendering, every language) ───────────
CLIENT = "web/client.js"


def test_outbound_axios_and_fetch(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": '{"dependencies": {"axios": "1"}}',
            CLIENT: """import axios from 'axios';
export function go(id, url) {
  axios.post(config.contextRoot + "/orders/confirm");
  axios.get(`${config.contextRoot}/orders/${id}`);
  fetch("/api/orders/" + id);
  axios.get(url);
  fetch("/api/plain");
}
""",
        }
    )
    assert _addr(_one(st, "api_call", CLIENT, 3)) == "${config.contextRoot}/orders/confirm"
    assert _addr(_one(st, "api_call", CLIENT, 4)) == "${config.contextRoot}/orders/{id}"  # concat = template
    assert _addr(_one(st, "api_call", CLIENT, 5)) == "/api/orders/{id}"  # a parameter
    assert _addr(_one(st, "api_call", CLIENT, 6)) == "${url}"  # variable only: named
    assert _addr(_one(st, "api_call", CLIENT, 7)) == "/api/plain"


def test_outbound_in_another_language(analyze_files) -> None:
    st = analyze_files(
        {
            "app/client.py": """import requests
def go(settings, oid):
    requests.get(f"{settings.BASE}/orders/{oid}")
    requests.post(settings.BASE + "/orders")
"""
        }
    )
    assert _addr(_one(st, "api_call", "app/client.py", 3)) == "${settings.BASE}/orders/{oid}"
    assert _addr(_one(st, "api_call", "app/client.py", 4)) == "${settings.BASE}/orders"


def test_outbound_jquery(analyze_files) -> None:
    st = analyze_files(
        {
            "web/page.js": """$(function () {
  $.ajax({ url: config.contextRoot + "/orders/confirm", type: "POST" });
  $.ajax({ url: "/orders/list" });
  $.ajax({ url: target });
});
"""
        }
    )
    assert _addr(_one(st, "api_call", "web/page.js", 2)) == "${config.contextRoot}/orders/confirm"
    assert _addr(_one(st, "api_call", "web/page.js", 3)) == "/orders/list"
    assert _addr(_one(st, "api_call", "web/page.js", 4)) == "${target}"


# ── inbound: Vert.x (Java and Groovy) ────────────────────────────────────────
VERTX = "src/main/java/a/Main.java"


def test_vertx_routes(analyze_files) -> None:
    st = analyze_files(
        {
            VERTX: """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
public class Main extends AbstractVerticle {
  static final String BASE = "/api";
  public void start() {
    Router router = Router.router(vertx);
    String pathPrefix = config().getString("p");
    router.get(pathPrefix + "/request").handler(this::h);
    router.get(pathPrefix + "/orders/:id").handler(this::h);
    router.get("/plain/:id").handler(this::h);
    router.get(pathPrefix).handler(this::h);
    router.get(BASE + "/x").handler(this::h);
    router.get(pathPrefix + "/a/" + pathPrefix).handler(this::h);
  }
  void h(Object c) {}
}
"""
        }
    )
    assert _addr(_one(st, "route", VERTX, 9)) == "${pathPrefix}/request"
    assert _addr(_one(st, "route", VERTX, 10)) == "${pathPrefix}/orders/:id"
    assert _addr(_one(st, "route", VERTX, 11)) == "/plain/:id"
    assert _addr(_one(st, "route", VERTX, 13)) == "/api/x"  # resolved constant
    assert _addr(_one(st, "route", VERTX, 14)) == "${pathPrefix}/a/${pathPrefix}"  # each marked
    # the whole path a local read once from configuration: named after that read
    assert _addr(_one(st, "route", VERTX, 12)) == "${config:p}"


def test_vertx_route_under_a_mount_with_unknown_prefix(analyze_files) -> None:
    st = analyze_files(
        {
            VERTX: """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
public class Main extends AbstractVerticle {
  public void start() {
    Router router = Router.router(vertx);
    Router api = Router.router(vertx);
    api.get("/users").handler(this::h);
    router.route(config().getString("p") + "/*").subRouter(api);
  }
  void h(Object c) {}
}
"""
        }
    )
    assert _addr(_one(st, "route", VERTX, 8)) == "${config:p}/users"


# ── inbound: Express ─────────────────────────────────────────────────────────
SERVER = "web/server.js"


def test_express_routes(analyze_files) -> None:
    st = analyze_files(
        {
            SERVER: """const express = require('express');
const app = express();
const BASE = process.env.BASE;
app.get(`${BASE}/users`, (req, res) => res.send());
app.get(BASE + '/users/:id', getUser);
app.get('/plain/:id', getUser);
app.get(`${BASE}`, getUser);
function getUser(req, res) {}
"""
        }
    )
    assert _addr(_one(st, "route", SERVER, 4)) == "${BASE}/users"
    assert _addr(_one(st, "route", SERVER, 5)) == "${BASE}/users/:id"  # concat = template
    assert _addr(_one(st, "route", SERVER, 6)) == "/plain/:id"
    assert _addr(_one(st, "route", SERVER, 7)) == "${BASE}"  # variable only: named


# ── inbound: Fastify ─────────────────────────────────────────────────────────
def test_fastify_runtime_and_literal_prefix(analyze_files) -> None:
    st = analyze_files(
        {
            "web/app.js": """const fastify = require('fastify')();
const config = require('./config');
fastify.register(require('./items'), { prefix: config.base });
fastify.register(require('./users'), { prefix: '/users' });
""",
            "web/items.js": """module.exports = async function (fastify) {
  fastify.get('/items/:id', async () => []);
};
""",
            "web/users.js": """module.exports = async function (fastify) {
  fastify.get('/', async () => []);
};
""",
        }
    )
    assert _addr(_one(st, "route", "web/items.js")) == "${config.base}/items/:id"
    assert _addr(_one(st, "route", "web/users.js")) == "/users"


def test_fastify_two_prefixes_stay_empty(analyze_files) -> None:
    st = analyze_files(
        {
            "web/app.js": """const fastify = require('fastify')();
fastify.register(require('./items'), { prefix: '/a' });
fastify.register(require('./items'), { prefix: '/b' });
""",
            "web/items.js": """module.exports = async function (fastify) {
  fastify.get('/items', async () => []);
};
""",
        }
    )
    assert _addr(_one(st, "route", "web/items.js")) is None


# ── inbound: NestJS ──────────────────────────────────────────────────────────
NEST = {
    "src/app.controller.ts": """import { Controller, Get } from '@nestjs/common';
@Controller('cats')
export class CatsController {
  @Get(':id')
  findOne() { return 1; }
}
""",
}


@pytest.mark.parametrize(
    "prefix, expected",
    [
        ("config.x", "${config.x}/cats/:id"),
        ("'api'", "/api/cats/:id"),
    ],
)
def test_nest_global_prefix(analyze_files, prefix, expected) -> None:
    st = analyze_files(
        {
            **NEST,
            "src/main.ts": f"""import {{ NestFactory }} from '@nestjs/core';
async function bootstrap() {{
  const app = await NestFactory.create(AppModule);
  app.setGlobalPrefix({prefix});
}}
""",
        }
    )
    assert _addr(_one(st, "route", "src/app.controller.ts")) == expected


# ── inbound: FastAPI ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "include, expected",
    [
        ("app.include_router(router, prefix=settings.API_PREFIX)", "${settings.API_PREFIX}/items/{item_id}"),
        ('app.include_router(router, prefix="/v1")', "/v1/items/{item_id}"),
        (
            'app.include_router(router, prefix="/a")\napp.include_router(router, prefix="/b")',
            None,
        ),
    ],
)
def test_fastapi_include_prefix(analyze_files, include, expected) -> None:
    st = analyze_files(
        {
            "app/main.py": f"""from fastapi import FastAPI, APIRouter
from .settings import settings
app = FastAPI()
router = APIRouter()
@router.get("/items/{{item_id}}")
def read_item(item_id: int):
    return item_id
{include}
"""
        }
    )
    assert _addr(_one(st, "route", "app/main.py")) == expected


# ── inbound: Ktor ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "prefix, expected",
    [
        ("prefixFromEnv()", "${prefixFromEnv}/v1/health"),
        ('"/api"', "/api/v1/health"),
    ],
)
def test_ktor_route_prefix(analyze_files, prefix, expected) -> None:
    st = analyze_files(
        {
            "src/App.kt": f"""import io.ktor.server.routing.*
import io.ktor.server.application.*
fun prefixFromEnv(): String = System.getenv("P")
fun Application.module() {{
  routing {{
    route({prefix}) {{
      route("/v1") {{
        get("/health") {{ }}
      }}
    }}
  }}
}}
"""
        }
    )
    assert _addr(_one(st, "route", "src/App.kt")) == expected


# ── inbound: LoopBack ────────────────────────────────────────────────────────
LB_CONTROLLER = """import {get, api} from '@loopback/rest';
@api({basePath: %s})
export class PingController {
  @get('/ping')
  ping() { return 'pong'; }
}
"""


@pytest.mark.parametrize(
    "base, expected",
    [
        ("BASE_PATH", "${BASE_PATH}/ping"),  # not a literal: unknown, never its source text
        ("'/v1'", "/v1/ping"),
    ],
)
def test_loopback_api_base_path(analyze_files, base, expected) -> None:
    st = analyze_files({"src/controllers/ping.controller.ts": LB_CONTROLLER % base})
    assert _addr(_one(st, "route", "src/controllers/ping.controller.ts")) == expected


def test_loopback_app_base_path_from_env(analyze_files) -> None:
    st = analyze_files(
        {
            "src/application.ts": """import {RestApplication} from '@loopback/rest';
export class App extends RestApplication {
  constructor() {
    super({rest: {basePath: process.env.BASE_PATH}});
  }
}
""",
            "src/controllers/ping.controller.ts": LB_CONTROLLER % "'/v1'",
        }
    )
    assert _addr(_one(st, "route", "src/controllers/ping.controller.ts")) == "${env:BASE_PATH}/v1/ping"


def test_vertx_sub_router_mounted_at_two_places_has_no_address(analyze_files) -> None:
    st = analyze_files(
        {
            VERTX: """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
public class Main extends AbstractVerticle {
  public void start() {
    Router router = Router.router(vertx);
    Router api = Router.router(vertx);
    api.get("/users").handler(this::h);
    router.route("/v1/*").subRouter(api);
    router.route("/v2/*").subRouter(api);
  }
  void h(Object c) {}
}
"""
        }
    )
    assert _addr(_one(st, "route", VERTX, 8)) is None


def test_jquery_prefix_plus_parameter_matches_axios(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": '{"dependencies": {"axios": "1"}}',
            "web/page.js": """import axios from 'axios';
function go(ctx, order) {
  $.ajax({ url: ctx + "/orders/" + order.id, type: "POST" });
  axios.post(ctx + "/orders/" + order.id);
}
""",
        }
    )
    jq, ax = sorted((s for s in st if s.semanticType == "api_call"), key=lambda s: s.startLine)
    assert _addr(jq) == _addr(ax) == "${ctx}/orders/{order.id}"


# ── jQuery: a URL built from strings and values reads as axios reads it ──────
@pytest.mark.parametrize(
    "url, expected",
    [
        ('config.contextRoot + "/orders/confirm"', "${config.contextRoot}/orders/confirm"),
        ('"/api/users/" + id', "/api/users/{id}"),
        ('"/api/users/" + user.id + "/roles"', "/api/users/{user.id}/roles"),
        ("`${base}/api/items`", "${base}/api/items"),
        ("`/api/users/${id}`", "/api/users/{id}"),
        ('"/a/" + id + ".json"', "/a/{id}.json"),
        ('"/a?q=" + q', "/a?q={q}"),
        ('"http://" + host + "/x"', "http://${host}/x"),
        ('"/api/a"', "/api/a"),
        ("u", "${u}"),  # only a variable: named
        ("base + suffix", "${base}${suffix}"),  # no literal part: each value named
        ('base + "/"', "${base}/"),
        ("`${host}${path}`", "${host}${path}"),
        ("process.env.API + '/x'", "${env:API}/x"),
        ("buildUrl(a, b) || fallback", None),  # no name: says nothing
    ],
)
def test_jquery_built_url_equals_axios(analyze_files, url, expected) -> None:
    st = analyze_files(
        {
            "package.json": '{"dependencies": {"axios": "1", "jquery": "3"}}',
            "web/page.js": f"""import axios from 'axios';
import $ from 'jquery';
axios.get({url});
$.get({url});
$.post({url}, data);
$.getJSON({url});
$.ajax({{ url: {url}, type: "post" }});
""",
        }
    )
    calls = sorted((s for s in st if s.semanticType == "api_call"), key=lambda s: s.startLine)
    assert [s.startLine for s in calls] == [3, 4, 5, 6, 7]
    assert {_addr(s) for s in calls} == {expected}


def test_kotlin_string_template_names_a_member_path(analyze_files) -> None:
    st = analyze_files(
        {
            "src/Client.kt": """import io.ktor.client.HttpClient
class Client(private val httpClient: HttpClient) {
  fun a(user: User, id: String) {
    httpClient.get("/api/users/${user.id}/roles")
    httpClient.get("/api/users/$id")
    httpClient.get("/api/users/${user.getId()}")
  }
}
"""
        }
    )
    calls = sorted((s for s in st if s.semanticType == "api_call"), key=lambda s: s.startLine)
    assert [_addr(s) for s in calls] == [
        "/api/users/{user.id}/roles",
        "/api/users/{id}",
        "/api/users/{param}",  # a call is not a name
    ]


# ── an address of named parts only: event bus, config, env, wrappers ────────────────────────
def test_event_bus_addresses_name_their_source(analyze_files) -> None:
    st = analyze_files({VERTX: """package a;
import io.vertx.core.AbstractVerticle;
public class Main extends AbstractVerticle {
  public void start() {
    String address = config().getJsonObject("api-verticle").getString("address");
    vertx.eventBus().send(address, "x");
    vertx.eventBus().consumer(config().getString("address"), this::a);
    vertx.eventBus().send(System.getenv("BUS") + ".orders", "y");
    vertx.eventBus().send("orders.create", "z");
  }
  void a(Object o) {}
}
"""})
    assert _addr(_one(st, "eventbus_send", VERTX, 6)) == "${config:api-verticle.address}"
    assert _addr(_one(st, "eventbus_consumer", VERTX, 7)) == "${config:address}"
    assert _addr(_one(st, "eventbus_send", VERTX, 8)) == "${env:BUS}.orders"
    assert _addr(_one(st, "eventbus_send", VERTX, 9)) == "orders.create"  # literal: unchanged


def test_a_wrapper_named_like_a_bus_does_not_take_its_first_argument(analyze_files) -> None:
    # `EventBusUtil.send(container, …)` holds "Bus" in its name; its first argument is no address
    st = analyze_files({VERTX: """package a;
import org.vertx.java.platform.Verticle;
public class Main extends Verticle {
  public void start() { EventBusUtil.send(container, vertx, "x"); }
}
"""})
    assert all(s.endpoint is None for s in st if s.semanticType == "eventbus_send")


def test_values_only_url_names_each_value(analyze_files) -> None:
    st = analyze_files({"src/main/java/a/FileApi.java": """package a;
import org.apache.http.client.methods.HttpPost;
public class FileApi {
  String fileApiPrefix, fileApiHost, filePort, fileApiPath;
  void up() {
    new HttpPost(fileApiPrefix + fileApiHost + ":" + filePort + fileApiPath);
    new HttpPost(fileApiPrefix + fileApiHost + ":" + filePort + "/file/upload");
    new HttpPost((fileApiPrefix + fileApiHost) + "/x");
  }
}
"""})
    path = "src/main/java/a/FileApi.java"
    assert _addr(_one(st, "api_call", path, 6)) == "${fileApiPrefix}${fileApiHost}:${filePort}${fileApiPath}"
    assert _addr(_one(st, "api_call", path, 7)) == "${fileApiPrefix}${fileApiHost}:${filePort}/file/upload"
    assert _addr(_one(st, "api_call", path, 8)) == "${param}/x"  # parenthesised: one value


def test_a_bare_middleware_is_still_no_mount(analyze_files) -> None:
    st = analyze_files({SERVER: """const express = require('express');
const app = express();
app.use(loggerMiddleware);
app.use('/api', apiRouter);
"""})
    assert [(s.startLine, s.endpoint) for s in st if s.semanticType == "route"] == [(4, "/api")]


def test_a_verb_on_a_request_builder_takes_the_body(analyze_files) -> None:
    st = analyze_files({"app/Calls.scala": """package example
import play.api.libs.ws.WSClient
class Calls(ws: WSClient) {
  def run(uri: String, body: String): Unit = {
    ws.url(uri).post(body)
  }
}
"""})
    assert [s.endpoint for s in st if s.semanticType == "api_call"] == ["${uri}"]


def test_ktor_value_only_path_needs_a_route_block(analyze_files) -> None:
    st = analyze_files({"src/App.kt": """import io.ktor.server.routing.*
import io.ktor.server.websocket.*
fun Route.delete(dao: Any, hash: (String) -> String) { }
fun Application.module(path: String, dao: Any) {
  routing {
    webSocket(path) { }
    delete(dao, { it })
  }
}
"""})
    eps = {s.text.split("(")[0].strip(): s.endpoint for s in st if s.semanticType == "route"}
    assert eps.get("webSocket") == "${path}"  # a route block: its path named
    assert eps.get("delete") is None  # the repo's own `delete(dao, …)`: no path named after `dao`


@pytest.mark.parametrize(
    "address, kept",
    [
        ("/api/{id?}", "/api/{id?}"),  # a framework's own parameter forms stay
        ("/files/{*path}", "/files/{*path}"),
        ("/x/{id:int}", "/x/{id:int}"),
        ("/{\n anz: `${base}/a`,\n asia: `/b`\n}", None),  # an object written where a path goes
    ],
)
def test_braces_holding_code_are_no_address(address, kept) -> None:
    assert informative(address) == kept
