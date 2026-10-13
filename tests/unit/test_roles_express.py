"""Express role table (docs/entry-point-roles/express.md): one test per row."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}
PKG = '{"dependencies": {"express": "4"}}'
HEAD = "import express from 'express';\nconst app = express();\n"


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def _at(stmts, path, line):
    return [s for s in stmts if s.path == path and s.startLine == line]


def _app(analyze_files, body: str, **extra: str):
    return analyze_files({"package.json": PKG, "src/app.js": HEAD + body, **extra})


def test_named_handler_is_a_binding(analyze_files) -> None:
    st = _app(analyze_files, "app.get('/users', listUsers);\n")
    [r] = _eps(st)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint, r.handler) == (
        "route",
        "GET",
        "route",
        "/users",
        "listUsers",
    )


def test_named_handler_line_is_its_declaration(analyze_files) -> None:
    st = _app(
        analyze_files,
        "app.get('/users', listUsers);\nfunction listUsers(req, res) {\n  res.send([]);\n}\n",
    )
    [r] = _eps(st)
    assert r.handlerLine == 4


def test_inline_handler_has_handler_line_only(analyze_files) -> None:
    st = _app(analyze_files, "app.post('/users',\n  (req, res) => { res.send('x'); });\n")
    [r] = _eps(st)
    assert (r.method, r.endpoint, r.handler, r.handlerLine) == ("POST", "/users", None, 4)


def test_wrapper_handler_is_the_innermost_function(analyze_files) -> None:
    st = _app(
        analyze_files,
        "app.put('/users',\n  asyncHandler(async (req, res) => { res.send('x'); }));\n",
    )
    [r] = _eps(st)
    assert (r.method, r.handler, r.handlerLine) == ("PUT", None, 4)


def test_route_chain_is_one_binding_per_verb(analyze_files) -> None:
    st = _app(analyze_files, "app.route('/book').get(getBook).post(postBook);\n")
    got = {(r.method, r.endpoint, r.handler) for r in _eps(st)}
    assert got == {("GET", "/book", "getBook"), ("POST", "/book", "postBook")}


def test_middleware_before_handler_are_guards(analyze_files) -> None:
    st = _app(analyze_files, "app.get('/health', requireAuth, rateLimit(), healthCheck);\n")
    [r] = _eps(st)
    assert r.handler == "healthCheck"
    assert r.guards == ["requireAuth", "rateLimit"]


@pytest.mark.xfail(strict=True, reason="a router mount is recorded as route/mount")
def test_router_mount_statement_stays_plain(analyze_files) -> None:
    st = _app(analyze_files, "const router = express.Router();\napp.use('/api', router);\n")
    assert _eps(_at(st, "src/app.js", 4)) == []
    assert _at(st, "src/app.js", 4), "the mount keeps its plain statement"


def test_router_factory_mount_prefixes_its_routes(analyze_files) -> None:
    st = _app(
        analyze_files,
        "import { usersRouter } from './users.js';\napp.use('/users', usersRouter());\n",
        **{
            "src/users.js": "import { Router } from 'express';\nexport function usersRouter() {\n"
            "  const router = Router();\n  router.get('/:id', getUser);\n  return router;\n}\n"
        },
    )
    [r] = [s for s in _eps(st) if s.path == "src/users.js"]
    assert r.endpoint == "/users/:id"


def test_router_instance_mount_prefixes_its_routes(analyze_files) -> None:
    st = _app(
        analyze_files,
        "import { itemsRouter } from './items.js';\napp.use('/items', itemsRouter);\n",
        **{
            "src/items.js": "import { Router } from 'express';\nexport const itemsRouter = Router();\n"
            "itemsRouter.get('/', listItems);\n"
        },
    )
    [r] = [s for s in _eps(st) if s.path == "src/items.js"]
    assert r.endpoint == "/items"


def test_same_file_router_mount_prefixes_its_routes(analyze_files) -> None:
    st = _app(
        analyze_files,
        "const router = express.Router();\nrouter.get('/ping', ping);\napp.use('/api', router);\n",
    )
    assert [r.endpoint for r in _eps(st) if r.method == "GET"] == ["/api/ping"]


def test_app_level_middleware_has_no_record(analyze_files) -> None:
    st = _app(analyze_files, "app.use(authenticate);\n")
    assert _eps(st) == []


def test_app_level_middleware_is_a_guard_on_later_routes(analyze_files) -> None:
    st = _app(analyze_files, "app.use(authenticate);\napp.get('/me', getMe);\n")
    [r] = _eps(st)
    assert r.guards == ["authenticate"]


@pytest.mark.xfail(strict=True, reason="an ambiguous app.use(path, x) is recorded as route/mount")
def test_ambiguous_use_is_wiring(analyze_files) -> None:
    st = _app(analyze_files, "app.use('/static', express.static('public'));\n")
    assert _eps(st) == []


@pytest.mark.xfail(strict=True, reason="the Apollo transport mount is recorded as POST /graphql")
def test_apollo_transport_mount_is_wiring(analyze_files) -> None:
    st = analyze_files(
        {
            "package.json": PKG,
            "src/app.js": HEAD + "import { expressMiddleware } from '@apollo/server/express4';\n"
            "app.use('/graphql', expressMiddleware(server));\n",
        }
    )
    assert _eps(st) == []


def test_unresolved_path_part_leaves_endpoint_empty(analyze_files) -> None:
    st = _app(
        analyze_files, "import { BASE } from './config.js';\napp.get(`${BASE}/users`, listUsers);\n"
    )
    [r] = _eps(st)
    assert r.endpoint in (None, "")


def test_settings_getter_is_none(analyze_files) -> None:
    st = _app(analyze_files, "const title = app.get('title');\n")
    assert _eps(st) == []


def test_listen_is_none(analyze_files) -> None:
    st = _app(analyze_files, "app.listen(3000);\n")
    assert _eps(st) == []


def test_http_client_call_is_outbound(analyze_files) -> None:
    st = _app(analyze_files, "fetch('/remote/api');\n")
    assert _eps(st) == []
    assert [s.endpoint for s in st if s.semanticType == "api_call"] == ["/remote/api"]
