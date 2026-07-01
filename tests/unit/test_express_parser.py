"""Express framework parser: call-based route detection, enrich-in-place, parentId
linkage, base reuse, override, settings-getter disambiguation."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_express.parser import ExpressParser
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
    return ExpressParser().parse_file(ctx)


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


def test_claims_selects_express() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(ExpressParser())
    assert registry.select("x.js", b"const e = require('express');").name == "typescript-express"
    assert registry.select("x.ts", b"import express from 'express';").name == "typescript-express"
    assert registry.select("x.ts", b"import s from 'express-session';").name == "typescript"  # not express
    assert registry.select("x.ts", b"const x = 1;").name == "typescript"  # plain TS -> base
    registry.clear()
