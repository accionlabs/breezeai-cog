"""Express framework parser: call-based route detection, enrich-in-place, parentId
linkage, base reuse, override, settings-getter disambiguation."""

from __future__ import annotations

import json
from unittest.mock import patch

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_express import routes as express_routes
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.schemas import FileRecord

SRC = b'''const express = require('express');
import externalRouter from './my-router';
const app = express();
const router = express.Router();
const r = Router();

app.get('/users/:id', (req, res) => { res.send('ok'); });
router.post('/users', createUser);
r.get('/router-instance', handleRouterInstance);
externalRouter.get('/imported-router', handleImportedRouter);
function registerRouter(userRouter) {
  userRouter.get('/parameter-router', handleParameterRouter);
}
app.all('/health', healthCheck);
app.use('/api', router);
app.route('/book').get(getBook).post(postBook);
router.route('/:id').put(updateBook).delete(deleteBook);

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


def _parse_source(tmp_path, name: str, source: bytes) -> FileRecord:
    p = tmp_path / name
    p.write_bytes(source)
    ctx = ParseContext(path=name, abs_path=p, source=source, repo_root=tmp_path,
                       capture_statements=True)
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
        ("GET", "/router-instance"),
        ("GET", "/imported-router"),
        ("GET", "/parameter-router"),
        ("ANY", "/health"),
        # Spec example: app.use('/api', router) → method ANY, routeKind mount,
        # endpoint /api. ANY is the method-enum value for an indeterminate verb.
        ("ANY", "/api"),         # includes app.use / Router().use mounts
        ("GET", "/book"),
        ("POST", "/book"),
        ("PUT", "/:id"),
        ("DELETE", "/:id"),
        ("DELETE", "/orders/:id"),
        ("GET", "/ping"),        # express.Router().get(...) direct-constructor receiver
    }
    assert routes[("POST", "/users")].handler == "createUser"
    assert routes[("GET", "/router-instance")].handler == "handleRouterInstance"
    assert routes[("GET", "/imported-router")].handler == "handleImportedRouter"
    assert routes[("GET", "/parameter-router")].handler == "handleParameterRouter"
    assert routes[("DELETE", "/orders/:id")].handler == "deleteOrder"
    assert routes[("ANY", "/api")].routeKind == "mount"
    assert routes[("ANY", "/api")].nodeType == "expression_statement"
    assert routes[("GET", "/book")].handler == "getBook"
    assert routes[("POST", "/book")].handler == "postBook"
    assert routes[("PUT", "/:id")].handler == "updateBook"
    assert routes[("DELETE", "/:id")].handler == "deleteBook"
    assert all(r.framework == "express" for r in routes.values())
    assert rec.framework == "express"
    book_routes = [
        s for s in rec.statements
        if s.semanticType == "route" and s.endpoint == "/book"
    ]
    assert [s.method for s in book_routes] == ["GET", "POST"]
    assert book_routes[0].nodeType == "expression_statement"
    assert book_routes[0].text == ".get(getBook)"
    assert book_routes[1].nodeType == "call_expression"
    assert book_routes[1].text == ".post(postBook)"
    assert len(book_routes) == 2


def test_non_route_member_calls_skip_receiver_resolution(tmp_path) -> None:
    src = b'''import express from 'express';
const app = express();
function handler(req, res) {
  res.json({});
  logger.info('request');
  items.map(transform);
  app.get('/ok', ok);
}
'''
    p = tmp_path / "server.ts"
    p.write_bytes(src)
    ctx = ParseContext(path="server.ts", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    with (
        patch.object(express_routes, "_router_bindings", wraps=express_routes._router_bindings)
        as binding_index,
        patch.object(
            express_routes,
            "_router_initializer",
            wraps=express_routes._router_initializer,
        ) as initializer_lookup,
    ):
        rec = TypeScriptParser().parse_file(ctx)

    assert binding_index.call_count == 1
    assert [call.args[0] for call in initializer_lookup.call_args_list] == ["app"]
    assert ("GET", "/ok") in {
        (s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"
    }


def test_receiver_constructor_expression_forms(tmp_path) -> None:
    src = b'''import express from 'express';
const router = require('express').Router();
const castApp = express() as Application;
const nonNullApp = express()!;
const parenthesizedApp = (express());

router.get('/cjs-router', cjsHandler);
castApp.get('/cast-app', castHandler);
nonNullApp.get('/non-null-app', nonNullHandler);
parenthesizedApp.get('/parenthesized-app', parenthesizedHandler);
'''
    rec = _parse_source(tmp_path, "receiver-constructors.ts", src)
    routes = {
        (s.method, s.endpoint)
        for s in rec.statements
        if s.semanticType == "route"
    }

    assert routes == {
        ("GET", "/cjs-router"),
        ("GET", "/cast-app"),
        ("GET", "/non-null-app"),
        ("GET", "/parenthesized-app"),
    }


def test_opaque_receiver_initializers_use_name_fallback(tmp_path) -> None:
    src = b'''import express from 'express';
{
  const app = initApp(express());
  app.get('/opaque-app-factory', opaqueAppHandler);
}
{
  const router = createRouter();
  router.post('/opaque-router-factory', opaqueRouterHandler);
}
'''
    rec = _parse_source(tmp_path, "opaque-receivers.ts", src)
    assert {
        (s.method, s.endpoint)
        for s in rec.statements
        if s.semanticType == "route"
    } == {
        ("GET", "/opaque-app-factory"),
        ("POST", "/opaque-router-factory"),
    }


def test_known_non_router_receivers_are_rejected(tmp_path) -> None:
    src = b'''import express from 'express';
const objectApp = {};
const arrayApp = [];
const literalApp = 1;
const functionApp = () => {};
objectApp.get('/object', handler);
arrayApp.get('/array', handler);
literalApp.get('/literal', handler);
functionApp.get('/function', handler);
'''
    rec = _parse_source(tmp_path, "non-router-receivers.ts", src)
    assert not [s for s in rec.statements if s.semanticType == "route"]


def test_deferred_router_assignment_and_uninitialized_receiver(tmp_path) -> None:
    src = b'''import express from 'express';
let deferredRouter;
deferredRouter = express.Router();
deferredRouter.get('/assigned-router', assignedHandler);
let routeApp;
routeApp.get('/uninitialized-app', handler);
let assignedObject;
assignedObject = {};
assignedObject.get('/assigned-object', handler);
'''
    rec = _parse_source(tmp_path, "deferred-receivers.ts", src)
    assert {
        (s.method, s.endpoint)
        for s in rec.statements
        if s.semanticType == "route"
    } == {("GET", "/assigned-router")}


def test_router_binding_scopes_and_shadowing(tmp_path) -> None:
    src = b'''import express from 'express';
const scopedRouter = express.Router();
function useOuterRouter() {
  scopedRouter.get('/outer-scope', outerHandler);
}
{
  const scopedRouter = {};
  scopedRouter.get('/shadowed-object', notARoute);
}
{
  const scopedRouter = createRouter();
  scopedRouter.post('/inner-scope-opaque', innerHandler);
}
scopedRouter.get('/outer-scope-again', outerHandler);
'''
    rec = _parse_source(tmp_path, "receiver-scopes.ts", src)
    assert {
        (s.method, s.endpoint)
        for s in rec.statements
        if s.semanticType == "route"
    } == {
        ("GET", "/outer-scope"),
        ("POST", "/inner-scope-opaque"),
        ("GET", "/outer-scope-again"),
    }


def test_binding_after_call_does_not_use_receiver_name_fallback(tmp_path) -> None:
    src = b'''import express from 'express';
futureRouter.get('/before-binding', notAValidRoute);
const futureRouter = {};
'''
    rec = _parse_source(tmp_path, "later-binding.ts", src)
    assert not [s for s in rec.statements if s.semanticType == "route"]


def test_chained_all_preserves_handler_and_middleware_guards(tmp_path) -> None:
    src = b'''import express from 'express';
const router = express.Router();
router.route('/secure').all(requireAuth, auditLog, finalHandler);
'''
    rec = _parse_source(tmp_path, "chained-all.ts", src)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].method == "ANY"
    assert routes[0].endpoint == "/secure"
    assert routes[0].handler == "finalHandler"
    assert routes[0].guards == ["requireAuth", "auditLog"]
    assert routes[0].authRequired is True
    assert routes[0].nodeType == "expression_statement"
    assert routes[0].text == ".all(requireAuth, auditLog, finalHandler)"


def test_multiple_chained_verbs_inside_function_remain_distinct(tmp_path) -> None:
    src = b'''import express from 'express';
const router = express.Router();
function register() {
  router.route('/nested').get(readNested).post(writeNested);
}
'''
    rec = _parse_source(tmp_path, "nested-chained-routes.ts", src)
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert [(s.method, s.endpoint, s.text) for s in routes] == [
        ("GET", "/nested", ".get(readNested)"),
        ("POST", "/nested", ".post(writeNested)"),
    ]


def test_non_router_route_chain_is_not_detected(tmp_path) -> None:
    src = b'''import express from 'express';
const thing = {};
thing.route('/x').get(handler);
'''
    rec = _parse_source(tmp_path, "non-router-chain.ts", src)
    assert not [s for s in rec.statements if s.semanticType == "route"]


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
    assert ("ANY", "/api/{version}") in routes        # mount path via template literal


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
    assert ("GET", "/sitemaps/{key}.txt") in routes  # template route, no longer lost
    assert ("GET", "*.*") in routes                  # wildcard route
    assert all(s.framework == "express" for s in rec.statements if s.semanticType == "route")
