"""Fastify role table (docs/entry-point-roles/fastify.md): one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

PKG = '{"dependencies": {"fastify": "5"}}'
HEAD = "import Fastify from 'fastify';\nconst app = Fastify();\n"
AUTOLOAD = (
    "import Fastify from 'fastify';\nimport AutoLoad from '@fastify/autoload';\n"
    "import path from 'node:path';\nconst app = Fastify();\n"
)


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _routes(stmts):
    return {(r.method, r.endpoint, r.handler) for r in _eps(stmts)}


def _app(analyze_files, body: str, **extra: str):
    return analyze_files({"package.json": PKG, "src/app.js": HEAD + body, **extra})


def test_named_handler_is_a_binding(analyze_files) -> None:
    st = _app(analyze_files, "app.get('/users', listUsers);\nfunction listUsers(req) {\n  return [];\n}\n")
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.handler, r.handlerLine,
            r.framework) == ("route", "GET", "route", "/users", "listUsers", 4, "fastify")


def test_inline_handler_has_handler_line_only(analyze_files) -> None:
    st = _app(analyze_files, "app.post('/users', { schema: {} },\n  async (request) => request.body);\n")
    [r] = _eps(st)
    assert (r.method, r.endpoint, r.handler, r.handlerLine) == ("POST", "/users", None, 4)


def test_handler_in_the_options_object(analyze_files) -> None:
    st = _app(analyze_files, "app.put('/users/:id', { handler: updateUser });\n")
    assert _routes(st) == {("PUT", "/users/:id", "updateUser")}


def test_route_object_is_one_binding_per_verb(analyze_files) -> None:
    st = _app(analyze_files,
              "app.route({ method: ['GET', 'POST'], url: '/items', handler: items });\n")
    assert _routes(st) == {("GET", "/items", "items"), ("POST", "/items", "items")}


def test_route_object_with_path_key(analyze_files) -> None:
    st = _app(analyze_files, "app.route({ method: 'DELETE', path: '/items/:id', handler: drop });\n")
    assert _routes(st) == {("DELETE", "/items/:id", "drop")}


def test_all_is_any(analyze_files) -> None:
    st = _app(analyze_files, "app.all('/proxy', proxy);\n")
    assert _routes(st) == {("ANY", "/proxy", "proxy")}


def test_route_hooks_are_guards(analyze_files) -> None:
    st = _app(analyze_files,
              "app.get('/me', { onRequest: [verifyJwt], preHandler: app.auth([isAdmin]) }, me);\n")
    [r] = _eps(st)
    assert (r.guards, r.authRequired) == (["verifyJwt", "app.auth"], True)


def test_instance_hook_guards_later_routes(analyze_files) -> None:
    st = _app(analyze_files,
              "app.get('/open', open);\napp.addHook('onRequest', authenticate);\n"
              "app.get('/closed', closed);\napp.addHook('onResponse', logIt);\n")
    by = {r.handler: r for r in _eps(st)}
    assert by["open"].guards is None
    assert (by["closed"].guards, by["closed"].authRequired) == (["authenticate"], None)


def test_websocket_route(analyze_files) -> None:
    st = _app(analyze_files, "app.get('/ws', { websocket: true }, onSocket);\n")
    [r] = _eps(st)
    assert (r.method, r.routeKind, r.endpoint, r.handler) == ("WEBSOCKET", "ws", "/ws", "onSocket")


def test_commonjs_app(analyze_files) -> None:
    st = analyze_files({"package.json": PKG, "server.js": (
        "const fastify = require('fastify')({ logger: true });\n"
        "fastify.get('/', async () => ({ hello: 'world' }));\n")})
    assert _routes(st) == {("GET", "/", None)}


def test_inline_plugin_prefix(analyze_files) -> None:
    st = _app(analyze_files,
              "app.register(async (r) => {\n  r.get('/:id', getUser);\n}, { prefix: '/users' });\n")
    assert _routes(st) == {("GET", "/users/:id", "getUser")}


def test_plugin_in_another_file_gets_its_prefix(analyze_files) -> None:
    st = _app(analyze_files,
              "import users from './users.js';\napp.register(users, { prefix: '/users' });\n",
              **{"src/users.js": "export default async function (fastify) {\n"
                                 "  fastify.get('/:id', getUser);\n}\n"})
    assert _routes(st) == {("GET", "/users/:id", "getUser")}


def test_nested_registration_composes(analyze_files) -> None:
    st = _app(analyze_files,
              "const v1 = require('./v1');\napp.register(v1, { prefix: '/v1' });\n",
              **{"src/v1.js": "module.exports = async function (f) {\n"
                              "  f.register(require('./users'), { prefix: '/users' });\n}\n",
                 "src/users.js": "module.exports = async function (f) {\n  f.get('/', list);\n}\n"})
    assert _routes(st) == {("GET", "/v1/users", "list")}


def test_registering_plugin_hooks_guard_its_routes(analyze_files) -> None:
    st = _app(analyze_files,
              "import users from './users.js';\napp.addHook('preHandler', auth);\n"
              "app.register(users, { prefix: '/users' });\n",
              **{"src/users.js": "export default async function (fastify) {\n"
                                 "  fastify.get('/', list);\n}\n"})
    [r] = _eps(st)
    assert (r.endpoint, r.guards) == ("/users", ["auth"])


def test_plugin_registered_at_two_prefixes_has_no_endpoint(analyze_files) -> None:
    st = _app(analyze_files,
              "import users from './users.js';\napp.register(users, { prefix: '/a' });\n"
              "app.register(users, { prefix: '/b' });\n",
              **{"src/users.js": "export default async function (fastify) {\n"
                                 "  fastify.get('/x', h);\n}\n"})
    assert _routes(st) == {("GET", None, "h")}


def test_unresolved_prefix_leaves_endpoint_empty(analyze_files) -> None:
    st = _app(analyze_files,
              "app.register(async (r) => {\n  r.get('/x', h);\n}, { prefix: config.base });\n")
    assert _routes(st) == {("GET", None, "h")}


def test_unresolved_path_leaves_endpoint_empty(analyze_files) -> None:
    st = _app(analyze_files, "app.get(settings.healthPath, health);\n")
    assert _routes(st) == {("GET", None, "health")}


def test_fastify_plugin_ignores_the_prefix(analyze_files) -> None:
    st = _app(analyze_files,
              "import fp from 'fastify-plugin';\nimport shared from './shared.js';\n"
              "app.register(shared, { prefix: '/ignored' });\n",
              **{"src/shared.js": "import fp from 'fastify-plugin';\n"
                                  "export default fp(async (fastify) => {\n"
                                  "  fastify.get('/health', health);\n});\n"})
    assert _routes(st) == {("GET", "/health", "health")}


def test_autoload_folder_is_the_prefix(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "src/app.js": AUTOLOAD + "app.register(AutoLoad, { dir: path.join(__dirname, 'routes') });\n",
        "src/routes/root.js": "module.exports = async function (fastify) {\n"
                              "  fastify.get('/', home);\n}\n",
        "src/routes/users/index.js": "module.exports = async function (fastify) {\n"
                                     "  fastify.get('/:id', getUser);\n}\n",
    })
    assert _routes(st) == {("GET", "/", "home"), ("GET", "/users/:id", "getUser")}


def test_autoload_options_prefix_and_auto_prefix(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "src/app.js": AUTOLOAD + "app.register(AutoLoad, {\n"
                                 "  dir: path.join(__dirname, 'routes'), options: { prefix: '/api' } });\n",
        "src/routes/status.js": "export const autoPrefix = '/_app';\n"
                                "export default async function (fastify) {\n"
                                "  fastify.get('/status', status);\n}\n",
        "src/routes/users/index.js": "export default async function (fastify) {\n"
                                     "  fastify.get('/', list);\n}\n",
    })
    assert _routes(st) == {("GET", "/api/_app/status", "status"), ("GET", "/api/users", "list")}


def test_autoload_without_folder_names(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "src/app.js": AUTOLOAD + "app.register(AutoLoad, {\n"
                                 "  dir: path.join(__dirname, 'routes'), dirNameRoutePrefix: false });\n",
        "src/routes/users/index.js": "export default async function (fastify) {\n"
                                     "  fastify.get('/users', list);\n}\n",
    })
    assert _routes(st) == {("GET", "/users", "list")}


def test_autoload_skips_siblings_of_an_index_file(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "src/app.js": AUTOLOAD + "app.register(AutoLoad, { dir: path.join(__dirname, 'routes') });\n",
        "src/routes/users/index.js": "export default async function (fastify) {\n"
                                     "  fastify.get('/', list);\n}\n",
        "src/routes/users/extra.ts": "import { FastifyInstance } from 'fastify';\n"
                                     "export default async function (fastify: FastifyInstance) {\n"
                                     "  fastify.get('/extra', extra);\n}\n",
    })
    assert _routes(st) == {("GET", "/users", "list"), ("GET", None, "extra")}


def test_autohooks_guard_the_folder(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "src/app.js": AUTOLOAD + "app.register(AutoLoad, {\n"
                                 "  dir: path.join(__dirname, 'routes'), autoHooks: true });\n",
        "src/routes/admin/autohooks.js": "module.exports = async function (fastify) {\n"
                                         "  fastify.addHook('onRequest', fastify.authenticate);\n}\n",
        "src/routes/admin/index.js": "module.exports = async function (fastify) {\n"
                                     "  fastify.get('/', dashboard);\n}\n",
    })
    [r] = _eps(st)
    assert (r.endpoint, r.guards) == ("/admin", ["fastify.authenticate"])


def test_unresolved_autoload_folder_leaves_unregistered_plugins_empty(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "src/app.js": AUTOLOAD + "app.register(AutoLoad, { dir: routesDir });\n",
        "src/routes/users.ts": "import { FastifyInstance } from 'fastify';\n"
                               "export default async function (fastify: FastifyInstance) {\n"
                               "  fastify.get('/users', list);\n}\n",
    })
    assert _routes(st) == {("GET", None, "list")}


def test_typed_plugin_nobody_registers_keeps_its_path(analyze_files) -> None:
    st = analyze_files({"package.json": PKG, "src/users.ts": (
        "import { FastifyPluginAsync } from 'fastify';\n"
        "const users: FastifyPluginAsync = async (app) => {\n  app.get('/users', list);\n};\n"
        "export default users;\n")})
    assert _routes(st) == {("GET", "/users", "list")}


def test_untyped_function_nobody_registers_is_none(analyze_files) -> None:
    st = analyze_files({"package.json": PKG, "src/cache.js": (
        "import fastify from 'fastify';\n"
        "export default function read(map, key) {\n  return map.get('k', key);\n}\n")})
    assert _eps(st) == []


def test_object_named_like_an_instance_is_none(analyze_files) -> None:
    st = analyze_files({"package.json": PKG, "src/store.js": (
        "import type { FastifyInstance } from 'fastify';\n"
        "const fastify = { get: (k, d) => d };\nfastify.get('/users', fallback);\n")})
    assert _eps(st) == []


def test_package_plugin_registration_is_none(analyze_files) -> None:
    st = _app(analyze_files, "import cors from '@fastify/cors';\napp.register(cors, { origin: true });\n")
    assert _eps(st) == []


def test_route_object_is_not_an_outbound_call(analyze_files) -> None:
    st = analyze_files({"package.json": PKG, "src/status.js": (
        "export default async function status(fastify) {\n"
        "  fastify.route({ method: 'GET', path: '/status', handler: onStatus });\n"
        "  async function onStatus() { return {}; }\n}\n")})
    assert not [s for s in st if s.semanticType == "api_call"]


def test_http_client_call_is_outbound(analyze_files) -> None:
    st = _app(analyze_files,
              "async function emails() {\n"
              "  return client.request({ method: 'GET', path: '/user/emails' });\n}\n")
    assert [(s.method, s.endpoint) for s in st if s.semanticType == "api_call"] == [
        ("GET", "/user/emails")]
    assert _eps(st) == []


def test_plugin_imported_through_a_path_alias(analyze_files) -> None:
    st = analyze_files({
        "package.json": PKG,
        "tsconfig.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@app/*": ["src/*"]}}}',
        "src/app.ts": HEAD + "import users from '@app/routes/users';\n"
                             "app.register(users, { prefix: '/users' });\n",
        "src/routes/users.ts": "import { FastifyInstance } from 'fastify';\n"
                               "export default async function (fastify: FastifyInstance) {\n"
                               "  fastify.get('/:id', getUser);\n}\n",
    })
    assert _routes(st) == {("GET", "/users/:id", "getUser")}


def test_function_called_with_an_instance_serves_under_its_prefix(analyze_files) -> None:
    st = _app(analyze_files,
              "import { usersRoutes } from './users.js';\n"
              "app.register(async (api) => {\n  await usersRoutes(api);\n}, { prefix: '/api' });\n",
              **{"src/users.ts": "import { FastifyInstance } from 'fastify';\n"
                                 "export async function usersRoutes(app: FastifyInstance) {\n"
                                 "  app.get('/users', list);\n}\n"})
    assert _routes(st) == {("GET", "/api/users", "list")}


def test_function_called_with_two_instances_has_no_endpoint(analyze_files) -> None:
    st = _app(analyze_files,
              "import { usersRoutes } from './users.js';\n"
              "app.register(async (a) => { await usersRoutes(a); }, { prefix: '/v1' });\n"
              "app.register(async (b) => { await usersRoutes(b); }, { prefix: '/v2' });\n",
              **{"src/users.ts": "import { FastifyInstance } from 'fastify';\n"
                                 "export async function usersRoutes(app: FastifyInstance) {\n"
                                 "  app.get('/users', list);\n}\n"})
    assert _routes(st) == {("GET", None, "list")}


def test_exported_plugin_also_called_in_its_own_file(analyze_files) -> None:
    st = _app(analyze_files,
              "import { tokens } from './tokens.js';\napp.register(tokens, { prefix: '/tokens' });\n",
              **{"src/tokens.ts": "import { FastifyInstance } from 'fastify';\n"
                                  "export const generic = async (s: FastifyInstance) => {\n"
                                  "  s.get('/:id', getToken);\n};\n"
                                  "export const tokens = async (s: FastifyInstance) => {\n"
                                  "  await generic(s);\n};\n"})
    assert _routes(st) == {("GET", "/tokens/:id", "getToken")}


def test_helper_called_from_two_plugins_does_not_join_them(analyze_files) -> None:
    st = _app(analyze_files,
              "import { a, b } from './routers.js';\n"
              "app.register(a, { prefix: '/a' });\napp.register(b, { prefix: '/b' });\n",
              **{"src/routers.ts": "import { FastifyInstance } from 'fastify';\n"
                                   "const audit = async (s: FastifyInstance, msg: string) => s.log.info(msg);\n"
                                   "export const a = async (s: FastifyInstance) => {\n"
                                   "  s.get('/x', async () => { await audit(s, 'a'); });\n};\n"
                                   "export const b = async (s: FastifyInstance) => {\n"
                                   "  s.get('/y', async () => { await audit(s, 'b'); });\n};\n"})
    assert {(r.method, r.endpoint) for r in _eps(st)} == {("GET", "/a/x"), ("GET", "/b/y")}
