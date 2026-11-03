"""Express framework parser: call-based route detection, enrich-in-place, parentId
linkage, base reuse, override, settings-getter disambiguation."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.schemas import FileRecord

SRC = b'''const express = require('express');
const app = express();
const router = express.Router();

app.get('/users/:id', (req, res) => { res.send('ok'); });
router.post('/users', createUser);
app.all('/health', healthCheck);
app.use('/api', router);
app.route('/book').get(getBook).post(postBook);

// not routes:
app.set('view engine', 'pug');
const title = app.get('title');       // settings getter (single string arg)
app.use(loggerMiddleware);            // bare middleware, no mount path

function register(server) {
  server.delete('/orders/:id', deleteOrder);
}

// direct-constructor receivers (no intermediate variable):
Router().use('/api', apiRouter);
express.Router().get('/ping', ping);
'''


def _parse(tmp_path, *, capture=True) -> FileRecord:
    p = tmp_path / "server.js"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="server.js", abs_path=p, source=SRC, repo_root=tmp_path,
                       capture_statements=capture)
    return TypeScriptParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with statement capture (spec A4).
    rec = _parse(tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_routes_detected(tmp_path) -> None:
    rec = _parse(tmp_path)
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {
        ("GET", "/users/:id"),
        ("POST", "/api/users"),  # router mounted at /api in the same file
        ("ANY", "/health"),       # app.all: every verb (Q13)
        ("GET", "/book"),        # app.route('/book').get(getBook): one record per verb
        ("POST", "/book"),       # ... .post(postBook); route('/book') itself is wiring
        ("DELETE", "/orders/:id"),
        ("GET", "/ping"),        # express.Router().get(...) direct-constructor receiver
        ("ANY", "/api"),         # Router().use('/api', apiRouter): apiRouter is unknown
    }
    # app.use('/api', router) mounts this file's router, whose routes carry /api: plain.
    # Router().use('/api', apiRouter) mounts a router found nowhere: a route / mount record.
    assert (routes[("ANY", "/api")].routeKind, routes[("ANY", "/api")].handler) == (
        "mount", "apiRouter")
    [mount] = [st for st in rec.statements if st.text == "app.use('/api', router);"]
    assert mount.semanticType is None
    assert routes[("POST", "/api/users")].handler == "createUser"
    assert routes[("DELETE", "/orders/:id")].handler == "deleteOrder"
    assert (routes[("GET", "/book")].handler, routes[("POST", "/book")].handler) == (
        "getBook", "postBook")
    assert all(r.framework == "express" for r in routes.values())
    assert rec.framework == "express"


def test_settings_getter_not_a_route(tmp_path) -> None:
    # app.get('title') / app.set(...) / bare app.use(mw) must not be misread as routes.
    rec = _parse(tmp_path)
    endpoints = {s.endpoint for s in rec.statements if s.semanticType == "route"}
    assert "title" not in endpoints
    assert "view engine" not in endpoints


def test_parentid_linkage(tmp_path) -> None:
    rec = _parse(tmp_path)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    fid = rec.id
    # Top-level routes enrich the base's file-parented expression statements.
    assert routes["/users/:id"].parentId == fid
    # A route inside register() is parented to that function.
    reg = next(f for f in rec.functions if f.name == "register")
    assert routes["/orders/:id"].parentId in (reg.id, fid)


def test_base_extraction_reused(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert "register" in {f.name for f in rec.functions}
    assert rec.language == "javascript"


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


# Template-literal route paths (the real cause of missed SSR/sitemap routes). Dynamic
# segments render to {param} placeholders; a leading interpolated base is stripped.
TEMPLATE_SRC = b'''const express = require('express');
const app = express();
const prefix = 'https://cdn.example.com';

app.get(`/sitemaps/${key}.txt`, sitemapHandler);
app.get(`/plain`, plainHandler);
app.get(`${prefix}/assets/${name}`, assetHandler);
app.use(`/api/${version}`, router);
'''


def test_template_literal_paths(tmp_path) -> None:
    p = tmp_path / "ssr.js"
    p.write_text(TEMPLATE_SRC.decode())
    ctx = ParseContext(path="ssr.js", abs_path=p, source=TEMPLATE_SRC, repo_root=tmp_path,
                       capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    routes = {(s.method, s.endpoint, s.handler) for s in rec.statements if s.semanticType == "route"}
    # An unresolved ${…} makes the address unknown: the route is kept, its endpoint empty —
    # never a {key} placeholder (a code name) or a path with its base dropped.
    assert ("GET", None, "sitemapHandler") in routes
    assert ("GET", "/plain", "plainHandler") in routes  # a template with no substitution
    assert ("GET", None, "assetHandler") in routes
    assert ("ANY", None, "router") in routes            # a mount whose template path is unknown


def test_template_path_resolves_through_constants(analyze_files) -> None:
    st = analyze_files({
        "package.json": '{"dependencies": {"express": "4"}}',
        "src/config.js": "export const API = '/api/v2';\n",
        "src/app.js": (
            "import express from 'express';\nimport { API } from './config.js';\n"
            "const app = express();\napp.get(`${API}/users`, listUsers);\n"
        ),
    })
    assert [s.endpoint for s in st if s.semanticType == "route"] == ["/api/v2/users"]


# R3: the Apollo GraphQL transport mount — app.use(path, expressMiddleware(server)).
# The path is a variable (resolved to its default) and the handler is the Apollo adapter.
APOLLO_SRC = b'''import express from 'express';
import { expressMiddleware } from '@apollo/server/express4';

export function createServer(graphqlPath = '/graphql') {
  const app = express();
  app.get('/health', (_req, res) => { res.end('OK'); });
  app.use('/api', router);                                  // ordinary sub-router mount
  app.use(graphqlPath, expressMiddleware(server, { context }));
  return app;
}
'''


def test_apollo_graphql_mount_detected(tmp_path) -> None:
    p = tmp_path / "server.ts"
    p.write_bytes(APOLLO_SRC)
    ctx = ParseContext(path="server.ts", abs_path=p, source=APOLLO_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    # /health stays a plain express route; /api (a router found nowhere) stays an express mount.
    assert ("GET", "/health") in routes and routes[("GET", "/health")].framework == "express"
    assert ("ANY", "/api") in routes and routes[("ANY", "/api")].routeKind == "mount"
    # the expressMiddleware mount is a POST /graphql route tagged graphql (path resolved
    # from the graphqlPath param default).
    gql = routes[("POST", "/graphql")]
    assert gql.framework == "graphql" and gql.routeKind == "route"


def test_apollo_mount_path_falls_back_to_convention(tmp_path) -> None:
    # If the mount path can't be resolved to a literal, default to the /graphql convention.
    src = b'''import { expressMiddleware } from '@apollo/server/express4';
app.use(cfg.gqlPath, expressMiddleware(server));
'''
    p = tmp_path / "s.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="s.ts", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    eps = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert ("POST", "/graphql") in eps


def test_express_is_additive_not_a_selecting_parser(tmp_path) -> None:
    # Express is no longer its own parser: a plain express file is owned by the base TS
    # parser, but express detection runs additively in extract → routes + framework label.
    registry.clear()
    registry.discover_builtin()
    assert registry.select("x.ts", b"import express from 'express';").name == "typescript"
    assert "typescript-express" not in {p.name for p in registry.registered()}
    registry.clear()

    src = b"import express from 'express';\nconst app = express();\napp.get('/x', h);\n"
    p = tmp_path / "srv.ts"
    p.write_bytes(src)
    rec = TypeScriptParser().parse_file(
        ParseContext(path="srv.ts", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True)
    )
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert ("GET", "/x") in {(s.method, s.endpoint) for s in routes}
    assert rec.framework == "express"


def test_express_routes_captured_in_angular_owned_file(tmp_path) -> None:
    # The #12 fix: a file legitimately BOTH Angular (imports @angular/ssr) and Express is
    # owned by the Angular parser, yet its express routes are still captured additively.
    from breezeai_cog.parsers.typescript_angular.parser import AngularParser

    src = b'''import '@angular/ssr/node';
import express from 'express';
const app = express();
app.get(`/sitemaps/${key}.txt`, sitemapHandler);
app.get('*.*', staticHandler);
'''
    p = tmp_path / "express-setup.service.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="express-setup.service.ts", abs_path=p, source=src,
                       repo_root=tmp_path, capture_statements=True)
    # Angular wins selection (both claim; tie → angular), and Angular does the extraction —
    # which now runs express detection additively.
    registry.clear()
    registry.discover_builtin()
    assert registry.select("express-setup.service.ts", src).name == "typescript-angular"
    registry.clear()

    rec = AngularParser().parse_file(ctx)
    routes = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert ("GET", None) in routes                   # template route kept, address unknown
    assert ("GET", "*.*") in routes                  # wildcard route
    assert all(s.framework == "express" for s in rec.statements if s.semanticType == "route")


# ── Router-instance mounts: app.use('/base', importedRouter) and same-file routers ──────────────
_PKG = '{"dependencies": {"express": "4"}}'


def _routes(analyze_files, files: dict[str, str]) -> dict[tuple[str, str | None], str | None]:
    """(path, method) → endpoint for every non-mount Express route."""
    return {
        (s.path, s.method): s.endpoint
        for s in analyze_files({"package.json": _PKG, **files})
        if s.semanticType == "route" and s.routeKind == "route"
    }


def test_instance_mount_named_default_and_aliased_exports(analyze_files) -> None:
    routes = _routes(analyze_files, {
        "src/index.js": (
            "import express from 'express';\n"
            "import { matchRouter } from './matches.js';\n"
            "import users from './users.js';\n"
            "import { orders as ord } from './orders.js';\n"
            "const app = express();\n"
            "app.use('/matches', matchRouter);\napp.use('/users', users);\napp.use('/orders', ord);\n"
        ),
        "src/matches.js": (
            "import { Router } from 'express';\nexport const matchRouter = Router();\n"
            "matchRouter.get('/:id', getMatch);\n"
        ),
        "src/users.js": (
            "import express from 'express';\nconst router = express.Router();\n"
            "router.post('/', createUser);\nexport default router;\n"
        ),
        "src/orders.js": (
            "import { Router } from 'express';\nconst r = Router();\nr.get('/', listOrders);\n"
            "export { r as orders };\n"
        ),
    })
    assert routes[("src/matches.js", "GET")] == "/matches/:id"
    assert routes[("src/users.js", "POST")] == "/users"
    assert routes[("src/orders.js", "GET")] == "/orders"


def test_instance_mount_middleware_import_is_not_a_prefix(analyze_files) -> None:
    # `auth` is a relative import but not a Router() → never a prefix; the router beside it is.
    routes = _routes(analyze_files, {
        "src/index.js": (
            "import express from 'express';\nimport { auth } from './auth.js';\n"
            "import { api } from './api.js';\nconst app = express();\n"
            "app.use('/secure', auth);\napp.use('/api', auth, api);\n"
        ),
        "src/auth.js": "export function auth(req, res, next) { next(); }\nauth.get = 1;\n",
        "src/api.js": (
            "import { Router } from 'express';\nexport const api = Router();\n"
            "api.get('/me', getMe);\n"
        ),
    })
    assert routes == {("src/api.js", "GET"): "/api/me"}


def test_instance_mounted_at_two_bases_has_no_address(analyze_files) -> None:
    routes = _routes(analyze_files, {
        "src/index.js": (
            "import express from 'express';\nimport { shared } from './shared.js';\n"
            "const app = express();\napp.use('/v1', shared);\napp.use('/v2', shared);\n"
        ),
        "src/shared.js": (
            "import { Router } from 'express';\nexport const shared = Router();\n"
            "shared.get('/status', status);\n"
        ),
    })
    assert routes[("src/shared.js", "GET")] is None  # served under both bases, never at /status


def test_same_file_nested_routers_compose(analyze_files) -> None:
    # `v1` is not named like a router; it is one because it is bound to express.Router().
    st = analyze_files({
        "package.json": _PKG,
        "src/index.js": (
            "import express from 'express';\nconst app = express();\n"
            "const api = express.Router();\nconst v1 = express.Router();\n"
            "v1.get('/ping', ping);\napi.get('/health', health);\n"
            "api.use('/v1', v1);\napp.use('/api', api);\n"
        ),
    })
    endpoints = sorted(s.endpoint for s in st if s.semanticType == "route" and s.routeKind == "route")
    assert endpoints == ["/api/health", "/api/v1/ping"]


def test_same_file_router_mounted_twice_has_no_address(analyze_files) -> None:
    routes = _routes(analyze_files, {
        "src/index.js": (
            "import express from 'express';\nconst app = express();\n"
            "const router = express.Router();\nrouter.get('/x', x);\n"
            "app.use('/a', router);\napp.use('/b', router);\n"
        ),
    })
    assert list(routes.values()) == [None]  # served under both bases, never at /x


# ── Handler and handlerLine ──────────────────────────────────────────────────────────────────
def test_handler_line_is_the_declaration_or_the_inline_function(analyze_files) -> None:
    st = analyze_files({
        "package.json": _PKG,
        "src/app.js": (
            "import express from 'express';\nimport { remote } from './remote.js';\n"
            "const app = express();\n"
            "app.get('/a', local);\n"                                   # 4
            "app.get('/b', remote);\n"                                  # 5
            "app.get('/c', (req, res) => res.send('c'));\n"             # 6
            "app.get('/d', asyncHandler(async (req, res) => {\n"        # 7
            "  res.send('d');\n}));\n"
            "app.get('/e', Users.list);\n"                              # 10
            "app.get('/f', make());\n"                                  # 11
            "function local(req, res) { res.send('a'); }\n"             # 12
        ),
    })
    got = {s.endpoint: (s.handler, s.handlerLine)
           for s in st if s.semanticType == "route" and s.routeKind == "route"}
    assert got == {
        "/a": ("local", 12),       # declared in this file
        "/b": ("remote", None),    # declared in another file: name, no line
        "/c": (None, 6),           # inline: line only
        "/d": (None, 7),           # wrapper unwrapped to the inner function
        "/e": ("Users.list", None),
        "/f": (None, None),        # a call with nothing to unwrap names no handler
    }


def test_route_chain_one_record_per_verb(analyze_files) -> None:
    st = analyze_files({
        "package.json": _PKG,
        "src/app.js": (
            "import express from 'express';\nconst app = express();\n"
            "const router = express.Router();\n"
            "router.route('/items/:id')\n"
            "  .get(getItem)\n"
            "  .put(auth, (req, res) => res.end())\n"
            "  .delete(asyncHandler(async (req, res) => res.end()));\n"
            "app.use('/api', router);\n"
            "function getItem(req, res) { res.end(); }\n"
        ),
    })
    routes = sorted(
        (s.method, s.endpoint, s.handler, s.handlerLine, s.guards)
        for s in st if s.semanticType == "route" and s.routeKind == "route"
    )
    assert routes == [
        ("DELETE", "/api/items/:id", None, 7, None),
        ("GET", "/api/items/:id", "getItem", 9, None),
        ("PUT", "/api/items/:id", None, 6, ["auth"]),
    ]
    assert len({s.id for s in st if s.semanticType == "route"}) == len(
        [s for s in st if s.semanticType == "route"])


# ── Global guards: middleware registered with use() before a route ────────────────────────────
def test_use_middleware_guards_later_routes(analyze_files) -> None:
    st = analyze_files({
        "package.json": _PKG,
        "src/app.js": (
            "import express from 'express';\nimport cors from 'cors';\n"
            "import { apiRoutes } from './api.js';\n"
            "const app = express();\n"
            "app.get('/early', early);\n"            # registered before any use → no guard
            "app.use(cors());\n"
            "app.use(authenticate);\n"
            "app.use(apiRoutes);\n"                  # relative import: may be a router → nothing
            "app.use(cfg.gqlPath, gqlAuth, gqlServer());\n"  # path in a variable → path-scoped
            "app.get('/me', requireRole('x'), getMe);\n"
            "const admin = express.Router();\n"
            "admin.use(audit);\n"
            "admin.get('/stats', stats);\n"
            "app.use('/admin', isAdmin, admin);\n"
            "function authenticate(req, res, next) { next(); }\n"
        ),
        "src/api.js": "import { Router } from 'express';\nexport const apiRoutes = Router();\n",
    })
    got = {s.endpoint: (s.guards, s.authRequired)
           for s in st if s.semanticType == "route" and s.routeKind == "route"}
    assert got["/early"] == (None, None)
    # global middleware first, then route-level; the app's own middleware (authenticate)
    # proves nothing certain about auth: listed, no claim
    assert got["/me"] == (["cors", "authenticate", "requireRole"], None)
    # parent app's middleware, then the mount's guard, then the router's own use()
    assert got["/admin/stats"] == (["cors", "authenticate", "isAdmin", "audit"], None)


# ── CommonJS: require() mounts and module.exports routers ─────────────────────────────────────
def test_commonjs_router_mounts_are_composed(analyze_files) -> None:
    st = analyze_files({
        "package.json": _PKG,
        "src/app.js": (
            "const express = require('express');\n"
            "const users = require('./routes/users');\n"
            "const { orders } = require('./routes/orders');\n"
            "const { admin: adm } = require('./routes/admin');\n"
            "const app = express();\n"
            "app.use('/users', users);\n"
            "app.use('/orders', orders);\n"
            "app.use('/admin', adm);\n"
            "app.use('/items', require('./routes/items'));\n"
            "app.use('/auth', require('./routes/auth')());\n"
            "app.use(users);\n"                       # a required router mounted at / → not a guard
            "app.get('/ping', ping);\n"
        ),
        "src/routes/users.js": (
            "const express = require('express');\nconst router = express.Router();\n"
            "router.get('/:id', getUser);\nmodule.exports = router;\n"
        ),
        "src/routes/orders.js": (
            "const { Router } = require('express');\nconst r = Router();\n"
            "r.get('/', listOrders);\nmodule.exports = { orders: r };\n"
        ),
        "src/routes/admin.js": (
            "const express = require('express');\nconst admin = express.Router();\n"
            "admin.get('/stats', stats);\nexports.admin = admin;\n"
        ),
        "src/routes/items.js": (
            "const express = require('express');\nconst router = express.Router();\n"
            "router.post('/', createItem);\nmodule.exports = router;\n"
        ),
        "src/routes/auth.js": (
            "const express = require('express');\n"
            "module.exports = function authRouter() {\n"
            "  const router = express.Router();\n  router.post('/login', login);\n  return router;\n};\n"
        ),
        "src/routes/mw.js": "module.exports = function audit(req, res, next) { next(); };\n",
    })
    got = {(s.path, s.method): s.endpoint for s in st
           if s.semanticType == "route" and s.routeKind == "route"}
    assert got[("src/routes/users.js", "GET")] == "/users/:id"
    assert got[("src/routes/orders.js", "GET")] == "/orders"
    assert got[("src/routes/admin.js", "GET")] == "/admin/stats"
    assert got[("src/routes/items.js", "POST")] == "/items"
    assert got[("src/routes/auth.js", "POST")] == "/auth/login"
    [ping] = [s for s in st if s.semanticType == "route" and s.endpoint == "/ping"]
    assert ping.guards is None


def test_commonjs_required_middleware_is_never_a_prefix(analyze_files) -> None:
    st = analyze_files({
        "package.json": _PKG,
        "src/app.js": (
            "const express = require('express');\nconst audit = require('./mw');\n"
            "const api = require('./api');\nconst app = express();\n"
            "app.use('/secure', audit);\napp.use('/api', audit, api);\n"
        ),
        "src/mw.js": "module.exports = function audit(req, res, next) { next(); };\n",
        "src/api.js": (
            "const express = require('express');\nconst api = express.Router();\n"
            "api.get('/me', me);\nmodule.exports = api;\n"
        ),
    })
    got = {s.path: s.endpoint for s in st if s.semanticType == "route" and s.routeKind == "route"}
    assert got == {"src/api.js": "/api/me"}


def test_mount_base_built_from_constants_folds_or_leaves_no_address(analyze_files) -> None:
    files = {
        "package.json": '{"dependencies": {"express": "4"}}',
        "src/config.js": "export const API = '/api';\n",
        "src/users.js": ("import { Router } from 'express';\nexport const users = Router();\n"
                         "users.get('/items', list);\n"),
        "src/orders.js": ("import { Router } from 'express';\nexport const orders = Router();\n"
                          "orders.get('/all', all);\n"),
        "src/app.js": (
            "import express, { Router } from 'express';\nimport { API } from './config';\n"
            "import { users } from './users';\nimport { orders } from './orders';\n"
            "const app = express();\n"
            "app.use(`${API}/v1`, users);\n"                  # constant from another file: folds
            "app.use(process.env.BASE + '/o', orders);\n"     # environment: does not fold
            "const local = Router();\nlocal.get('/ping', ping);\n"
            "app.use(API + '/local', local);\n"               # same file, concatenation: folds
            "const dyn = Router();\ndyn.get('/x', x);\n"
            "app.use(`${cfg.base}`, dyn);\n"                  # same file, unresolved: no address
        ),
    }
    st = analyze_files(files)
    got = {(s.path, s.handler): s.endpoint for s in st
           if s.semanticType == "route" and s.method == "GET"}
    assert got == {
        ("src/users.js", "list"): "/api/v1/items",
        ("src/orders.js", "all"): None,
        ("src/app.js", "ping"): "/api/local/ping",
        ("src/app.js", "x"): None,
    }


def test_require_express_router_is_a_router_whatever_its_name(analyze_files) -> None:
    # The constructor decides, not the variable: `r` is no router-like name.
    st = analyze_files({
        "package.json": _PKG,
        "src/d.js": (
            "const r = require('express').Router();\n"
            "r.get('/d', (req, res) => res.json([]));\n"
            "module.exports = r;\n"
        ),
    })
    got = {(s.path, s.method, s.endpoint) for s in st if s.semanticType == "route"}
    assert got == {("src/d.js", "GET", "/d")}


def test_router_of_another_object_is_not_a_router(analyze_files) -> None:
    # `$.Router()` / `require('other').Router()` build no Express router.
    st = analyze_files({
        "package.json": _PKG,
        "src/a.js": (
            "const q = $.Router();\nq.get('/a', (req, res) => res.json([]));\n"
            "const w = require('other').Router();\nw.get('/b', (req, res) => res.json([]));\n"
        ),
    })
    assert [s for s in st if s.semanticType == "route"] == []


_MULTI_USERS = "import { Router } from 'express';\nconst router = Router();\nrouter.get('/users', list);\nexport default router;\n"
_MULTI_ORDERS = "import { Router } from 'express';\nconst router = Router();\nrouter.get('/orders', list);\nexport default router;\n"


def _route_rows(analyze_files, files: dict[str, str]):
    st = analyze_files({"package.json": _PKG, **files})
    return {(s.routeKind, s.method, s.endpoint, tuple(s.guards or ()), s.handler)
            for s in st if s.semanticType == "route"}


def test_two_routers_in_one_mount_both_get_the_prefix(analyze_files) -> None:
    rows = _route_rows(analyze_files, {
        "src/users.ts": _MULTI_USERS,
        "src/orders.ts": _MULTI_ORDERS,
        "src/app.ts": (
            "import express from 'express';\nimport users from './users';\n"
            "import orders from './orders';\nimport { auth } from 'some-auth';\n"
            "const app = express();\napp.use('/api', auth, users, orders);\n"
        ),
    })
    # both routers in the repo → no mount record; the other router is never a guard
    assert rows == {("route", "GET", "/api/users", ("auth",), "list"),
                    ("route", "GET", "/api/orders", ("auth",), "list")}


def test_one_router_per_mount_still_works(analyze_files) -> None:
    rows = _route_rows(analyze_files, {
        "src/users.ts": _MULTI_USERS,
        "src/orders.ts": _MULTI_ORDERS,
        "src/app.ts": (
            "import express from 'express';\nimport users from './users';\n"
            "import orders from './orders';\nimport { auth } from 'some-auth';\n"
            "const app = express();\napp.use('/api', auth, users);\napp.use('/api', orders);\n"
        ),
    })
    assert rows == {("route", "GET", "/api/users", ("auth",), "list"),
                    ("route", "GET", "/api/orders", (), "list")}


def test_multi_router_mount_with_a_router_at_two_bases(analyze_files) -> None:
    # `users` is also mounted at /v2: where it is served is unknown, so its routes have no
    # address, and the multi-router mount keeps its record (its address is recorded nowhere else).
    rows = _route_rows(analyze_files, {
        "src/users.ts": _MULTI_USERS,
        "src/orders.ts": _MULTI_ORDERS,
        "src/app.ts": (
            "import express from 'express';\nimport users from './users';\n"
            "import orders from './orders';\nconst app = express();\n"
            "app.use('/api', users, orders);\napp.use('/v2', users);\n"
        ),
    })
    assert ("route", "GET", None, (), "list") in rows
    assert ("route", "GET", "/api/orders", (), "list") in rows
    assert any(r[0] == "mount" and r[2] == "/api" for r in rows)


def test_same_file_two_routers_in_one_mount(analyze_files) -> None:
    rows = _route_rows(analyze_files, {
        "src/app.ts": (
            "import express, { Router } from 'express';\nimport { auth } from 'some-auth';\n"
            "const app = express();\nconst a = Router();\nconst b = Router();\n"
            "a.get('/x', hx);\nb.get('/y', hy);\napp.use('/api', auth, a, b);\n"
        ),
    })
    assert rows == {("route", "GET", "/api/x", ("auth",), "hx"),
                    ("route", "GET", "/api/y", ("auth",), "hy")}


def _handler_locations(analyze_files, files: dict[str, str]):
    st = analyze_files({"package.json": _PKG, **files})
    return {s.endpoint: (s.handler, s.handlerPath, s.handlerLine)
            for s in st if s.semanticType == "route" and s.routeKind == "route"}


def test_member_namespace_and_commonjs_handlers_are_located(analyze_files) -> None:
    got = _handler_locations(analyze_files, {
        "src/users.ts": "export function list(req, res) {\n  res.json([]);\n}\n",
        "src/orders.ts": (
            "export class OrderController {\n"
            "  list(req, res) { res.json([]); }\n"
            "  static health(req, res) { res.send('ok'); }\n"
            "}\n"
        ),
        "src/legacy.js": "module.exports = {\n  show(req, res) { res.send('x'); },\n};\n",
        "src/ping.js": "exports.ping = function (req, res) {\n  res.send('pong');\n};\n",
        "src/app.ts": (
            "import express from 'express';\n"
            "import * as users from './users';\n"
            "import { OrderController } from './orders';\n"
            "const ctrl = require('./legacy');\n"
            "const { ping } = require('./ping');\n"
            "const oc = new OrderController();\n"
            "const app = express();\n"
            "app.get('/users',  users.list);\n"
            "app.get('/orders', oc.list);\n"
            "app.get('/legacy', ctrl.show);\n"
            "app.get('/ping',   ping);\n"
            "app.get('/static', OrderController.health);\n"
        ),
    })
    assert got == {
        "/users": ("users.list", "src/users.ts", 1),
        "/orders": ("oc.list", "src/orders.ts", 2),
        "/legacy": ("ctrl.show", "src/legacy.js", 2),
        "/ping": ("ping", "src/ping.js", 1),
        "/static": ("OrderController.health", "src/orders.ts", 3),
    }


def test_ambiguous_member_handlers_stay_unlocated(analyze_files) -> None:
    got = _handler_locations(analyze_files, {
        "src/a.ts": "export function list(req, res) {}\n",
        "src/b.ts": "export function list(req, res) {}\n",
        "src/barrel.ts": "export * from './a';\nexport * from './b';\n",  # `list` twice
        "src/orders.ts": (
            "export class C {\n  get x() { return 1; }\n  set x(v) {}\n"
            "  list(req, res) {}\n  static health(req, res) {}\n}\n"
        ),
        "src/app.ts": (
            "import express from 'express';\nimport * as api from './barrel';\n"
            "import { C } from './orders';\nlet oc = new C();\nlet oc = new C();\n"
            "const one = new C();\nconst app = express();\n"
            "app.get('/a', api.list);\n"      # exported by two modules → ambiguous
            "app.get('/b', oc.list);\n"       # instance declared twice → ambiguous
            "app.get('/c', C.list);\n"        # an instance method through the class: not static
            "app.get('/d', one.health);\n"    # a static method through an instance
            "app.get('/e', one.x);\n"         # getter + setter → no single method
            "app.get('/f', one.missing);\n"   # no such method (maybe inherited)
        ),
    })
    assert {ep: loc for ep, (_, *loc) in got.items()} == {
        ep: [None, None] for ep in ("/a", "/b", "/c", "/d", "/e", "/f")}


def test_local_class_and_instance_handlers(analyze_files) -> None:
    got = _handler_locations(analyze_files, {
        "src/app.ts": (
            "import express from 'express';\n"
            "class Ctl {\n  list = (req, res) => res.json([]);\n  static ok(req, res) {}\n}\n"
            "const ctl = new Ctl();\nconst app = express();\n"
            "app.get('/l', ctl.list);\napp.get('/o', Ctl.ok);\n"
        ),
    })
    assert got == {"/l": ("ctl.list", None, 3), "/o": ("Ctl.ok", None, 4)}


def test_two_mounts_on_one_line_stay_apart(analyze_files) -> None:
    # Mount sites are told apart by column too: one line, two `use` calls, each its own guards.
    rows = _route_rows(analyze_files, {
        "src/users.ts": _MULTI_USERS,
        "src/orders.ts": _MULTI_ORDERS,
        "src/app.ts": (
            "import express from 'express';\nimport users from './users';\n"
            "import orders from './orders';\nimport { auth } from 'some-auth';\n"
            "const app = express();\napp.use('/api', auth, users); app.use('/api', orders);\n"
        ),
    })
    assert rows == {("route", "GET", "/api/users", ("auth",), "list"),
                    ("route", "GET", "/api/orders", (), "list")}


def test_mount_record_never_lists_a_router_as_a_guard(analyze_files) -> None:
    # `b` is also mounted at /other: the /api mount is not wiring, so it keeps a record — whose
    # handler is no single router (two), and whose guards are not the routers.
    st = analyze_files({"package.json": _PKG, "src/app.js": (
        "const express = require('express');\nconst app = express();\n"
        "const a = express.Router();\nconst b = express.Router();\n"
        "a.get('/x', hx);\nb.get('/y', hy);\n"
        "app.use('/api', auth, a, b);\napp.use('/other', b);\n"
    )})
    mounts = {(s.endpoint, s.handler, tuple(s.guards or ())) for s in st if s.routeKind == "mount"}
    assert ("/api", None, ("auth",)) in mounts
    assert all("a" not in g and "b" not in g for _, _, g in mounts)


def test_cross_file_multi_router_mount_record(analyze_files) -> None:
    rows = _route_rows(analyze_files, {
        "src/users.ts": _MULTI_USERS,
        "src/orders.ts": _MULTI_ORDERS,
        "src/app.ts": (
            "import express from 'express';\nimport users from './users';\n"
            "import orders from './orders';\nimport { auth } from 'some-auth';\n"
            "const app = express();\napp.use('/api', auth, users, orders);\napp.use('/v2', users);\n"
        ),
    })
    assert ("mount", "ANY", "/api", ("auth",), None) in rows  # two routers: no single handler
    assert ("route", "GET", "/api/orders", ("auth",), "list") in rows


def test_local_router_is_not_a_guard_of_a_mounted_router(analyze_files) -> None:
    rows = _route_rows(analyze_files, {
        "src/users.js": (
            "const express = require('express');\nconst r = express.Router();\n"
            "r.get('/users', list);\nmodule.exports = r;\n"
        ),
        "src/app.js": (
            "const express = require('express');\nconst users = require('./users');\n"
            "const app = express();\nconst a = express.Router();\na.get('/a', ha);\n"
            "app.use('/api', users, a);\n"
        ),
    })
    assert ("route", "GET", "/api/users", (), "list") in rows
    assert ("route", "GET", "/api/a", (), "ha") in rows


def test_accessors_are_not_handlers(analyze_files) -> None:
    got = _handler_locations(analyze_files, {
        "src/orders.ts": "export class C {\n  get list() { return () => 1; }\n}\n",
        "src/legacy.js": "module.exports = {\n  get show() { return () => 1; },\n};\n",
        "src/app.ts": (
            "import express from 'express';\nimport { C } from './orders';\n"
            "const ctrl = require('./legacy');\nconst one = new C();\nconst app = express();\n"
            "app.get('/a', one.list);\napp.get('/b', ctrl.show);\n"
        ),
    })
    assert {ep: loc for ep, (_, *loc) in got.items()} == {"/a": [None, None], "/b": [None, None]}


def test_mount_guards_are_the_middleware_before_each_router(analyze_files) -> None:
    # Express runs a mount's arguments in order: `requireAuth` runs only after `users` passes the
    # request on, so it guards `orders` and not `users`; `notFound` runs after both, so it guards
    # neither.
    rows = _route_rows(analyze_files, {
        "src/users.ts": _MULTI_USERS,
        "src/orders.ts": _MULTI_ORDERS,
        "src/app.ts": (
            "import express from 'express';\nimport users from './users';\n"
            "import orders from './orders';\nimport { requireAuth, notFound } from 'some-auth';\n"
            "const app = express();\napp.use('/api', users, requireAuth, orders, notFound);\n"
        ),
    })
    assert rows == {("route", "GET", "/api/users", (), "list"),
                    ("route", "GET", "/api/orders", ("requireAuth",), "list")}


def test_same_file_mount_guards_are_the_middleware_before_each_router(analyze_files) -> None:
    rows = _route_rows(analyze_files, {
        "src/app.js": (
            "const express = require('express');\nconst app = express();\n"
            "const pub = express.Router();\nconst priv = express.Router();\n"
            "pub.get('/health', (req, res) => res.send('ok'));\n"
            "priv.get('/me', (req, res) => res.send('me'));\n"
            "app.use('/api', cors(), pub, requireAuth, priv, notFound);\n"
        ),
    })
    got = {(endpoint, guards) for _, _, endpoint, guards, _ in rows}
    assert ("/api/health", ("cors",)) in got
    assert ("/api/me", ("cors", "requireAuth")) in got
    assert not any("notFound" in guards for _, guards in got)


def test_factory_mount_guards_are_the_middleware_before_it(analyze_files) -> None:
    st = analyze_files({
        "package.json": _PKG,
        "src/users.js": (
            "import { Router } from 'express';\nexport function usersRouter() {\n"
            "  const router = Router();\n  router.get('/:id', getUser);\n  return router;\n}\n"
        ),
        "src/app.js": (
            "import express from 'express';\nimport { usersRouter } from './users.js';\n"
            "const app = express();\napp.use('/users', requireAuth, usersRouter(), notFound);\n"
        ),
    })
    [r] = [s for s in st if s.semanticType == "route" and s.path == "src/users.js"]
    assert (r.endpoint, r.guards) == ("/users/:id", ["requireAuth"])


def test_commonjs_re_export_of_a_require_is_never_the_require_line(analyze_files) -> None:
    got = _handler_locations(analyze_files, {
        "src/handlers/getUser.js": "module.exports = function getUser(req, res) { res.send(1); };\n",
        "src/handlers/listUsers.js": "export default function listUsers(req, res) {\n  res.send([]);\n}\n",
        "src/handlers/util.js": "exports.ping = function (req, res) {\n  res.send('pong');\n};\n",
        "src/handlers/index.js": (
            "const getUser = require('./getUser');\n"      # `module.exports = fn`: unknown
            "const listUsers = require('./listUsers');\n"  # a default export: located
            "const ping = require('./util').ping;\n"       # a member of the module: located
            "const lodash = require('lodash');\n"          # a package: unknown
            "module.exports = { getUser, listUsers, ping, lodash };\n"
        ),
        "src/app.js": (
            "const express = require('express');\nconst app = express();\n"
            "const handlers = require('./handlers');\n"
            "app.get('/u', handlers.getUser);\napp.get('/l', handlers.listUsers);\n"
            "app.get('/p', handlers.ping);\napp.get('/x', handlers.lodash);\n"
        ),
    })
    assert got == {
        "/u": ("handlers.getUser", None, None),
        "/l": ("handlers.listUsers", "src/handlers/listUsers.js", 1),
        "/p": ("handlers.ping", "src/handlers/util.js", 1),
        "/x": ("handlers.lodash", None, None),
    }


def test_a_local_shadowing_a_top_level_name_is_not_located(analyze_files) -> None:
    got = _handler_locations(analyze_files, {
        "src/ctl.ts": (
            "export class UserController {\n  list(req, res) {}\n}\n"
            "export class AdminController {\n  list(req, res) {}\n}\n"
        ),
        "src/app.ts": (
            "import express from 'express';\n"
            "import { UserController, AdminController } from './ctl';\n"
            "const controller = new UserController();\n"
            "function handler(req, res) {}\n"
            "const app = express();\n"
            "app.get('/users', controller.list);\n"          # the top-level instance: located
            "function admin() {\n"
            "  const controller = new AdminController();\n"  # shadows it
            "  app.get('/admin', controller.list);\n"
            "}\n"
            "function unrelated() {\n  const controller = 1;\n}\n"
            "app.get('/top', handler);\n"                     # a sibling scope's local: located
        ),
    })
    assert got["/users"] == ("controller.list", "src/ctl.ts", 2)
    assert got["/admin"] == ("controller.list", None, None)
    assert got["/top"][2] == 4


def test_class_methods_are_indexed_only_for_exported_classes(tmp_path) -> None:
    from breezeai_cog.parsers.typescript.declarations import _collect
    from breezeai_cog.parsers.treesitter import parse_source

    src = (b"class Hidden {\n  a() {}\n}\nclass Ctl {\n  b() {}\n}\n"
           b"export { Ctl as Controller };\nexport default class Main {\n  c() {}\n}\n")
    root = parse_source("typescript", src).root_node
    got = _collect(root, src, "src/x.ts", tmp_path)
    assert got is not None and set(got.methods) == {"Ctl", "Main"}


def test_aliased_and_default_exported_class_handlers_are_located(analyze_files) -> None:
    got = _handler_locations(analyze_files, {
        "src/ctl.ts": "class Ctl {\n  static b(req, res) {}\n}\nexport { Ctl as Controller };\n",
        "src/main.ts": "export default class Main {\n  static c(req, res) {}\n}\n",
        "src/app.ts": (
            "import express from 'express';\nimport { Controller } from './ctl';\n"
            "import Main from './main';\nconst app = express();\n"
            "app.get('/b', Controller.b);\napp.get('/c', Main.c);\n"
        ),
    })
    assert got == {"/b": ("Controller.b", "src/ctl.ts", 2), "/c": ("Main.c", "src/main.ts", 2)}


def test_loop_switch_and_catch_bindings_shadow_only_their_own_body(analyze_files) -> None:
    got = _handler_locations(analyze_files, {
        "src/users.ts": "export function list(req, res) {}\n",
        "src/app.ts": (
            "import express from 'express';\nimport { list } from './users';\n"
            "const app = express();\n"
            "function setup(xs, k) {\n"
            "  for (let list = 0; list < 1; list++) {}\n"   # the loop's own `list`
            "  switch (k) { case 1: { const list = 2; } }\n"
            "  app.get('/after', list);\n"                   # the import again: located
            "  for (const list of xs) {\n"
            "    app.get('/loop', list);\n"                  # the loop variable: not located
            "  }\n"
            "  try { k(); }\n"
            "  catch (list) {\n"
            "    app.get('/catch', list);\n"                 # the caught error: not located
            "  }\n"
            "}\n"
        ),
    })
    assert got["/after"] == ("list", "src/users.ts", 1)
    assert got["/loop"][1:] == (None, None)
    assert got["/catch"][1:] == (None, None)


def test_es_export_of_a_require_is_never_the_require_line(analyze_files) -> None:
    got = _handler_locations(analyze_files, {
        "src/users.ts": "export function list(req, res) {\n  res.json([]);\n}\n",
        "src/index.ts": "export const users = require('./users');\nexport const pkg = require('lodash');\n",
        "src/app.ts": (
            "import express from 'express';\nimport { users, pkg } from './index';\n"
            "const app = express();\napp.get('/u', users.list);\napp.get('/p', pkg.list);\n"
        ),
    })
    assert got["/p"][1:] == (None, None)
    assert got["/u"] == ("users.list", None, None)  # never the `require` line of src/index.ts
