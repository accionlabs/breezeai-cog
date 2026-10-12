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
        ("ALL", "/health"),
        (None, "/api"),          # app.use / Router().use mount (both endpoints are /api)
        ("GET", "/book"),        # app.route('/book').get(getBook): one record per verb
        ("POST", "/book"),       # ... .post(postBook); route('/book') itself is wiring
        ("DELETE", "/orders/:id"),
        ("GET", "/ping"),        # express.Router().get(...) direct-constructor receiver
    }
    assert routes[("POST", "/api/users")].handler == "createUser"
    assert routes[("DELETE", "/orders/:id")].handler == "deleteOrder"
    assert routes[(None, "/api")].routeKind == "mount"
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
    assert (None, None, "router") in routes             # mount path via template literal


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
    # /health stays a plain express route; /api stays an express mount.
    assert ("GET", "/health") in routes and routes[("GET", "/health")].framework == "express"
    assert (None, "/api") in routes and routes[(None, "/api")].routeKind == "mount"
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


def test_instance_mounted_at_two_bases_keeps_its_own_path(analyze_files) -> None:
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
    assert routes[("src/shared.js", "GET")] == "/status"


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


def test_same_file_router_mounted_twice_keeps_its_own_path(analyze_files) -> None:
    routes = _routes(analyze_files, {
        "src/index.js": (
            "import express from 'express';\nconst app = express();\n"
            "const router = express.Router();\nrouter.get('/x', x);\n"
            "app.use('/a', router);\napp.use('/b', router);\n"
        ),
    })
    assert list(routes.values()) == ["/x"]


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
