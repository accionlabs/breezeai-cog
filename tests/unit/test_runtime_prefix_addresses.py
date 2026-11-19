"""One rule for an address whose leading part is a runtime value (``parsers.addresses``):
``endpoint`` stays empty and ``endpointSuffix`` holds the known tail — for every framework and
both directions. A path parameter, a variable-only address and a resolved prefix are unchanged."""

from __future__ import annotations

import pytest

from breezeai_cog.parsers.addresses import join_suffix, marked_address, runtime_suffix


def _one(stmts, semantic: str, path: str, line: int | None = None):
    found = [
        s
        for s in stmts
        if s.semanticType == semantic and s.path == path and (line is None or s.startLine == line)
    ]
    assert len(found) == 1, found
    return found[0]


def _addr(stmt) -> tuple[str | None, str | None]:
    return stmt.endpoint, stmt.endpointSuffix


# ── the helpers ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "rendered, tail",
    [
        ("{contextRoot}/orders/confirm", "/orders/confirm"),
        ("{base}/orders/{id}", "/orders/{id}"),
        ("{host}:{port}/x", "/x"),
        ("{scheme}://{host}/x", "/x"),
        ("{base}", None),
        ("{base}{path}", None),
        ("{base}/", None),
        ("/api/{id}", None),
        (None, None),
    ],
)
def test_runtime_suffix(rendered, tail) -> None:
    assert runtime_suffix(rendered) == tail


def test_marked_address_and_join_suffix() -> None:
    assert marked_address("/api", "/x") == ("/api/x", None)
    assert marked_address("\0", "/x") == (None, "/x")
    assert marked_address("/a\0/v1", "/x") == (None, "/v1/x")
    assert marked_address("\0", None) == (None, None)
    assert join_suffix("/api/", "x") == "/api/x"
    assert join_suffix("/", "") is None


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
    assert _addr(_one(st, "api_call", CLIENT, 3)) == (None, "/orders/confirm")
    assert _addr(_one(st, "api_call", CLIENT, 4)) == (None, "/orders/{id}")  # concat = template
    assert _addr(_one(st, "api_call", CLIENT, 5)) == ("/api/orders/{id}", None)  # a parameter
    assert _addr(_one(st, "api_call", CLIENT, 6)) == (None, None)  # variable only
    assert _addr(_one(st, "api_call", CLIENT, 7)) == ("/api/plain", None)


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
    assert _addr(_one(st, "api_call", "app/client.py", 3)) == (None, "/orders/{oid}")
    assert _addr(_one(st, "api_call", "app/client.py", 4)) == (None, "/orders")


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
    assert _addr(_one(st, "api_call", "web/page.js", 2)) == (None, "/orders/confirm")
    assert _addr(_one(st, "api_call", "web/page.js", 3)) == ("/orders/list", None)
    assert _addr(_one(st, "api_call", "web/page.js", 4)) == (None, None)


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
    assert _addr(_one(st, "route", VERTX, 9)) == (None, "/request")
    assert _addr(_one(st, "route", VERTX, 10)) == (None, "/orders/:id")
    assert _addr(_one(st, "route", VERTX, 11)) == ("/plain/:id", None)
    assert _addr(_one(st, "route", VERTX, 13)) == ("/api/x", None)  # resolved constant
    assert _addr(_one(st, "route", VERTX, 14)) == (None, None)  # unknown after the tail too
    assert [
        s for s in st if s.path == VERTX and s.startLine == 12 and s.semanticType == "route"
    ] == []  # variable only: not a route, as before


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
    assert _addr(_one(st, "route", VERTX, 8)) == (None, "/users")


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
    assert _addr(_one(st, "route", SERVER, 4)) == (None, "/users")
    assert _addr(_one(st, "route", SERVER, 5)) == (None, "/users/:id")  # concat = template
    assert _addr(_one(st, "route", SERVER, 6)) == ("/plain/:id", None)
    assert _addr(_one(st, "route", SERVER, 7)) == (None, None)  # variable only


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
    assert _addr(_one(st, "route", "web/items.js")) == (None, "/items/:id")
    assert _addr(_one(st, "route", "web/users.js")) == ("/users", None)


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
    assert _addr(_one(st, "route", "web/items.js")) == (None, None)


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
        ("config.x", (None, "/cats/:id")),
        ("'api'", ("/api/cats/:id", None)),
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
        ("app.include_router(router, prefix=settings.API_PREFIX)", (None, "/items/{item_id}")),
        ('app.include_router(router, prefix="/v1")', ("/v1/items/{item_id}", None)),
        (
            'app.include_router(router, prefix="/a")\napp.include_router(router, prefix="/b")',
            (None, None),
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
        ("prefixFromEnv()", (None, "/v1/health")),
        ('"/api"', ("/api/v1/health", None)),
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
        ("BASE_PATH", (None, "/ping")),  # not a literal: unknown, never its source text
        ("'/v1'", ("/v1/ping", None)),
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
    assert _addr(_one(st, "route", "src/controllers/ping.controller.ts")) == (None, "/v1/ping")


def test_vertx_sub_router_mounted_at_two_places_has_no_suffix(analyze_files) -> None:
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
    assert _addr(_one(st, "route", VERTX, 8)) == (None, None)


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
    assert _addr(jq) == _addr(ax) == (None, "/orders/{id}")
