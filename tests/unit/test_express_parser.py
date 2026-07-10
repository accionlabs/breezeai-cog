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
    # Routes are statements — only emitted with --capture-statements (spec A4).
    rec = _parse(tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_routes_detected(tmp_path) -> None:
    rec = _parse(tmp_path)
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {
        ("GET", "/users/:id"),
        ("POST", "/users"),
        ("ALL", "/health"),
        (None, "/api"),          # app.use / Router().use mount (both endpoints are /api)
        (None, "/book"),         # app.route group
        ("DELETE", "/orders/:id"),
        ("GET", "/ping"),        # express.Router().get(...) direct-constructor receiver
    }
    assert routes[("POST", "/users")].handler == "createUser"
    assert routes[("DELETE", "/orders/:id")].handler == "deleteOrder"
    assert routes[(None, "/api")].routeKind == "mount"
    assert routes[(None, "/book")].routeKind == "route"
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
    routes = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/sitemaps/{key}.txt") in routes   # interpolation → {key} placeholder
    assert ("GET", "/plain") in routes                # a template with no substitution
    assert ("GET", "/assets/{name}") in routes        # leading ${prefix} base stripped
    assert (None, "/api/{version}") in routes         # mount path via template literal


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
    assert ("GET", "/sitemaps/{key}.txt") in routes  # template route, no longer lost
    assert ("GET", "*.*") in routes                  # wildcard route
    assert all(s.framework == "express" for s in rec.statements if s.semanticType == "route")
