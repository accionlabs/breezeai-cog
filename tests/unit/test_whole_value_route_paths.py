"""A route whose whole path is one value cog cannot read is recorded with that value marked and
named after its source (``addresses.py``), only on an app or router the file certainly holds and
only with a handler (Vert.x: java_vertx/events.py; Express: typescript_express/routes.py)."""

from __future__ import annotations

VERTX = "src/main/java/a/Main.java"
APP = "src/app.js"


def _routes(stmts, path):
    return {s.startLine: (s.method, s.endpoint, s.handler)
            for s in stmts if s.path == path and s.semanticType == "route"}


def test_vertx(analyze_files) -> None:
    got = _routes(analyze_files({VERTX: """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.core.http.HttpMethod;
import io.vertx.ext.web.Router;
public class Main extends AbstractVerticle {
  private String basePath;
  public void start() {
    Router router = Router.router(vertx);
    String path = config().getString("orders.path");
    router.get(path).handler(this::list);
    router.get(config().getString("orders.list")).handler(this::list);
    router.get(this.basePath).handler(this::list);
    router.post(System.getenv("ORDERS")).handler(this::list);
    router.get(path);
    router.route(HttpMethod.GET, path).handler(this::list);
    router.get(helper()).handler(this::list);
    java.util.Map<String, String> m = new java.util.HashMap<>();
    m.get(path);
  }
  String helper() { return "/x"; }
  void list(Object c) {}
}
"""}), VERTX)
    assert got[10] == ("GET", "${config:orders.path}", "list")  # a local read once from config
    # its own key: the same path read twice would be one endpoint, grouped into line 10's route
    assert got[11] == ("GET", "${config:orders.list}", "list")
    assert got[12] == ("GET", "${basePath}", "list")  # `this.` dropped
    assert got[13] == ("POST", "${env:ORDERS}", "list")
    assert 14 not in got  # no handler
    assert 15 not in got  # the first argument is the verb
    assert 16 not in got  # another call: not read as a path
    assert 18 not in got  # not a router


def test_vertx_reassigned_local_names_itself(analyze_files) -> None:
    got = _routes(analyze_files({VERTX: """package a;
import io.vertx.core.AbstractVerticle;
import io.vertx.ext.web.Router;
public class Main extends AbstractVerticle {
  public void start() {
    Router router = Router.router(vertx);
    String path = config().getString("a");
    if (path == null) path = "/fallback";
    router.get(path).handler(this::list);
  }
  void list(Object c) {}
}
"""}), VERTX)
    assert got[9] == ("GET", "${path}", "list")


def test_express(analyze_files) -> None:
    got = _routes(analyze_files({APP: """const express = require('express');
const app = express();
const ordersRouter = express.Router();
const ordersPath = process.env.ORDERS_PATH;
function listOrders(req, res) {}
ordersRouter.get('/list', listOrders);
app.get(ordersPath, listOrders);
app.get(process.env.ORDERS_PATH, listOrders);
app.get(routes.orders, listOrders);
app.use(ordersPath, ordersRouter);
app.use(authMiddleware);
app.use(logger, otherRouter);
app.use(helmet(), cors());
cache.get(key, onHit);
app.get(settingName);
app.get(listOrders, listOrders);
const otherRouter = express.Router();
"""}), APP)
    assert got[6] == ("GET", "${env:ORDERS_PATH}/list", "listOrders")  # mounted at the env read
    assert got[7] == ("GET", "${env:ORDERS_PATH}", "listOrders")  # a const read once from env
    assert got[8] == ("GET", "${env:ORDERS_PATH}", "listOrders")
    assert got[9] == ("GET", "${routes.orders}", "listOrders")  # the whole member path
    assert 10 not in got  # the mount of a router of this file: on its routes
    for line in (11, 12, 13, 14, 15, 16):  # middleware, not a router, a setting, a function
        assert line not in got


def test_express_typed_parameter_and_shadowing(analyze_files) -> None:
    got = _routes(analyze_files({"src/routes.ts": """import express, { Express } from 'express';
const base = process.env.BASE;
export function register(app: Express, base: string) {
  app.get(base, (req, res) => res.send('ok'));
}
export function other(server: Server) {
  server.get(base, (req, res) => res.send('ok'));
}
"""}), "src/routes.ts")
    assert got[4] == ("GET", "${base}", None)  # the parameter shadows the env read
    assert 7 not in got  # not typed as an Express app


def test_express_router_mounted_at_a_runtime_base(analyze_files) -> None:
    got = _routes(analyze_files({APP: """const express = require('express');
const app = express();
const api = express.Router();
const admin = express.Router();
api.get('/x', (req, res) => {});
admin.get('/y', (req, res) => {});
app.use(BASE + '/api', api);
api.use(process.env.ADMIN, admin);
"""}), APP)
    assert got[5][1] == "${BASE}/api/x"  # the marked base comes first
    assert got[6][1] == "${BASE}/api/${env:ADMIN}/y"  # nested under its parent's base


def test_vertx2_route_matcher(analyze_files) -> None:
    got = _routes(analyze_files({VERTX: """package a;
import org.vertx.java.core.http.RouteMatcher;
import org.vertx.java.platform.Verticle;
public class Main extends Verticle {
  public void start() {
    RouteMatcher rm = new RouteMatcher();
    String path = container.config().getString("orders");
    rm.get(path, this::list);
  }
  void list(Object r) {}
}
"""}), VERTX)
    assert got[8][:2] == ("GET", "${config:orders}")  # 2.x `container.config()` alike
