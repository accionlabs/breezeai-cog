"""A name in a TypeScript / JavaScript address folds to a constant only when the file shows where
the value comes from (``parsers/typescript/scopes.py``): the file declares it as a ``const`` /
``readonly`` / enum literal, or imports it from the file that does. Anything else — a value read at
runtime, a parameter, a ``let``, a name the file neither declares nor imports — stays marked
``${name}`` (or leaves the address empty), never another file's constant of the same name."""

from __future__ import annotations

import pytest

PKG = '{"dependencies": {"express": "4", "fastify": "4"}}'
CONFIG = "export const BASE = '/api';\n"
OTHER = "export const BASE = '/admin';\n"
SERVER = "src/server.js"
HEAD = "const express = require('express');\nconst app = express();\n"


def _route(st, path: str = SERVER, line: int | None = None) -> str | None:
    found = [s for s in st if s.semanticType == "route" and s.path == path
             and (line is None or s.startLine == line)]
    assert len(found) == 1, [(s.startLine, s.endpoint) for s in found]
    return found[0].endpoint


@pytest.mark.parametrize(
    "decl, expected",
    [
        # 1. this file reads BASE at runtime: another file's '/admin' is not it
        ("const BASE = process.env.API_BASE;", "${BASE}/u"),
        # 2–3. imported (ES, CommonJS) from the file that declares it
        ("import { BASE } from './config';", "/api/u"),
        ("const { BASE } = require('./config');", "/api/u"),
        # an import alias reads the export it names
        ("import { BASE as API } from './config'; const BASE = API;", "${BASE}/u"),
        # 5. this file's own constant, even when another file has another value
        ("const BASE = '/v1';", "/v1/u"),
        # 6. neither declared nor imported (a browser global, a typo): no other file's value
        ("", "${BASE}/u"),
        # 7. imported from a package: a repository file declaring BASE is not it
        ("import { BASE } from 'some-package';", "${BASE}/u"),
        # 8. imported through a re-export: not followed
        ("import { BASE } from './barrel';", "${BASE}/u"),
        # a `let` / `var` may be reassigned: its first value is no constant
        ("let BASE = '/api';", "${BASE}/u"),
        ("var BASE = '/api';", "${BASE}/u"),
    ],
)
def test_a_name_folds_only_where_the_file_shows_its_constant(analyze_files, decl, expected) -> None:
    st = analyze_files({
        "package.json": PKG,
        "src/config.js": CONFIG,
        "src/other.js": OTHER,
        "src/barrel.js": "export { BASE } from './config';\n",
        SERVER: f"{decl}\n{HEAD}app.get(BASE + '/u', h);\nfunction h(req, res) {{}}\n",
    })
    assert _route(st) == expected


def test_the_template_form_reads_the_same_constant(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "src/other.js": OTHER,
        SERVER: "const BASE = process.env.API_BASE;\n" + HEAD
        + "app.get(`${BASE}/orders`, h);\nfunction h(req, res) {}\n",
    })
    assert _route(st) == "${BASE}/orders"  # never '/admin/orders'


def test_a_namespace_import_reads_its_modules_constant(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "src/config.js": CONFIG,
        SERVER: "import * as cfg from './config';\n" + HEAD
        + "app.get(cfg.BASE + '/u', h);\nfunction h(req, res) {}\n",
    })
    assert _route(st) == "/api/u"


def test_a_tsconfig_alias_import_reads_its_files_constant(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@app/*": ["src/*"]}}}',
        "src/config.ts": CONFIG,
        "src/server.ts": "import { BASE } from '@app/config';\n" + HEAD
        + "app.get(BASE + '/u', h);\nfunction h(req: any, res: any) {}\n",
    })
    assert _route(st, "src/server.ts") == "/api/u"


@pytest.mark.parametrize(
    "body",
    [
        "function mount(BASE) { app.get(BASE + '/u', h); }",            # a parameter
        "function mount() { if (x) { var BASE = read(); } app.get(BASE + '/u', h); }",  # hoisted
        "function mount() { const BASE = read(); app.get(`${BASE}/u`, h); }",  # a block local
        "for (const BASE of bases) { app.get(BASE + '/u', h); }",          # a loop binding
    ],
)
def test_a_local_hides_the_files_own_constant(analyze_files, body) -> None:
    st = analyze_files({
        "package.json": PKG,
        SERVER: "const BASE = '/api';\n" + HEAD + body + "\nfunction h(req, res) {}\n",
    })
    assert _route(st) == "${BASE}/u"


def test_a_fastify_inline_prefix_never_takes_another_files_constant(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "web/other.js": "export const PREFIX = '/v2';\n",
        "web/app.js": "const fastify = require('fastify')();\n"
        "const PREFIX = process.env.API_PREFIX;\n"
        "fastify.register(async (f) => { f.get('/inline', async () => 1); }, "
        "{ prefix: PREFIX + '/x' });\n",
    })
    assert _route(st, "web/app.js") == "${PREFIX}/x/inline"


def test_a_nest_controller_path_reads_the_imported_file(analyze_files) -> None:
    common = "import { Controller, Get } from '@nestjs/common';\n"
    st = analyze_files({
        "package.json": '{"dependencies": {"@nestjs/common": "10", "@nestjs/core": "10"}}',
        "src/paths.ts": "export const USERS = 'users';\n",
        "src/other.ts": "export const ORDERS = 'orders';\n",
        "src/a.controller.ts": common + "import { USERS } from './paths';\n"
        "@Controller(USERS)\nexport class A {\n  @Get(':id')\n  one() {}\n}\n",
        "src/b.controller.ts": common
        + "@Controller(ORDERS)\nexport class B {\n  @Get(':id')\n  one() {}\n}\n",
    })
    assert _route(st, "src/a.controller.ts") == "/users/:id"
    assert _route(st, "src/b.controller.ts") is None  # ORDERS is not imported here
