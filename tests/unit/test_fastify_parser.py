"""Regression tests for additive TypeScript Fastify route detection."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.additive import all_detectors
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.parsers.typescript.imports import TsAliasIndex
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.parsers.typescript_fastify import routes as fastify_routes
from breezeai_cog.parsers.typescript_fastify.routes import detect_fastify_routes
from breezeai_cog.schemas import FileRecord


def test_fastify_is_registered_as_additive_detector() -> None:
    detectors = {detector.name: detector for detector in all_detectors()["typescript"]}

    assert "fastify" in detectors
    assert detectors["fastify"].frameworks == ("fastify",)
    assert detectors["fastify"].skip_fixtures is True


def _with_factory_import_for_fixture(source: bytes) -> bytes:
    if b'from "fastify"' in source or b"from 'fastify'" in source or b'require("fastify")' in source:
        return source
    if b"function Fastify(" in source or b"function fastify(" in source:
        return source
    if b"Fastify(" in source:
        return b'import Fastify from "fastify";\n' + source
    if b"fastify(" in source:
        return b'import fastify from "fastify";\n' + source
    return source


def _parse_typescript(
    source: bytes,
    path: str,
    *,
    capture_statements: bool = True,
    resolution_index=None,
):
    source = _with_factory_import_for_fixture(source)
    context = ParseContext(
        path=path,
        abs_path=Path(path),
        source=source,
        repo_root=Path("."),
        capture_statements=capture_statements,
        resolution_index=resolution_index,
    )
    return TypeScriptParser().parse_file(context)


def _detect_routes(
    source: bytes,
    path: str = "app.ts",
    *,
    inject_factory_import: bool = True,
    resolution_index=None,
):
    if inject_factory_import:
        source = _with_factory_import_for_fixture(source)
    root = parse_source("typescript", source).root_node
    record = FileRecord(id=path, path=path, type="code", language="typescript", loc=1)
    return detect_fastify_routes(
        root,
        source,
        path,
        record,
        seen_ids=set(),
        resolution_index=resolution_index,
    )


def test_detects_route_from_known_fastify_instance() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const fastify = Fastify();\n"
        b"fastify.get('/users', async (request, reply) => ({ users: [] }));"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/users")]
    assert routes[0].framework == "fastify"


def test_detects_route_from_esm_default_fastify_factory() -> None:
    source = (
        b'import * as fastify from "fastify";\n'
        b"const app = fastify.default();\n"
        b"app.get('/users', async (request, reply) => ({ users: [] }));"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/users")]


def test_detects_route_from_require_default_fastify_factory() -> None:
    source = (
        b'const app = require("fastify").default();\n'
        b"app.get('/users', async (request, reply) => ({ users: [] }));"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/users")]


@pytest.mark.parametrize("factory_name", ["Fastify", "fastify"])
def test_detects_route_from_commonjs_fastify_factory_binding(factory_name: str) -> None:
    source = (
        f'const {factory_name} = require("fastify");\n'
        f"const app = {factory_name}();\n"
        "app.get('/users', handler);\n"
    ).encode()

    record = _parse_typescript(source, "app.js")
    routes = [statement for statement in record.statements if statement.semanticType == "route"]

    assert [(route.method, route.endpoint, route.framework) for route in routes] == [
        ("GET", "/users", "fastify")
    ]
    assert record.framework == "fastify"


def test_fastify_route_enriches_existing_expression_statement() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const fastify = Fastify();\n"
        b"fastify.get('/first', firstHandler); fastify.get('/second', secondHandler);"
    )

    record = _parse_typescript(source, "server.ts")
    routes = [statement for statement in record.statements if statement.framework == "fastify"]

    assert [(route.endpoint, route.nodeType) for route in routes] == [
        ("/first", "expression_statement"),
        ("/second", "expression_statement"),
    ]
    assert record.framework == "fastify"


def test_parse_file_captures_fastify_route_end_to_end() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const app = Fastify();\n"
        b"app.get('/users', handler);\n"
    )

    record = _parse_typescript(source, "app.ts")

    routes = [statement for statement in record.statements if statement.framework == "fastify"]
    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/users")]
    assert record.framework == "fastify"


def test_ignores_unrelated_get_method() -> None:
    source = (
        b'import axios from "axios";\n'
        b"const client = axios;\n"
        b"client.get('/users', async () => []);"
    )

    assert _detect_routes(source) == []


def test_ignores_locally_defined_fastify_factory_names() -> None:
    sources = (
        b"function Fastify() { return {}; }\n"
        b"const app = Fastify();\n"
        b"app.get('/not-fastify', handler);",
        b"function fastify() { return {}; }\n"
        b"const app = fastify();\n"
        b"app.get('/not-fastify', handler);",
    )

    assert all(_detect_routes(source) == [] for source in sources)


def test_ignores_unbound_fastify_factory_name() -> None:
    source = b"const app = Fastify(); app.get('/not-fastify', handler);"

    assert _detect_routes(source, inject_factory_import=False) == []


def test_detects_post_route() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const fastify = Fastify();\n"
        b"fastify.post('/users', async (request, reply) => ({ created: true }));"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("POST", "/users")]


@pytest.mark.parametrize(
    "class_body",
    [
        b"app = Fastify();\n  add() { this.app.get('/users', handler); }",
        b"constructor() { this.app = Fastify(); }\n"
        b"  add() { this.app.get('/users', handler); }",
    ],
)
def test_detects_fastify_route_on_initialized_this_property(class_body: bytes) -> None:
    source = b'import Fastify from "fastify";\nclass Routes {\n  ' + class_body + b"\n}"

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/users")]


def test_ignores_unrelated_this_property_route_call() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"class Routes {\n"
        b"  app = {};\n"
        b"  add() { this.app.get('/not-fastify', handler); }\n"
        b"}"
    )

    assert _detect_routes(source) == []


def test_regex_route_has_null_endpoint_and_is_marked_regex() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const app = Fastify();\n"
        b"app.get(/users\\/.*/, handler);\n"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint, route.isRegex) for route in routes] == [
        ("GET", None, True)
    ]


def test_unresolved_shorthand_route_is_captured_with_null_endpoint() -> None:
    source = (
        b"const app = Fastify();\n"
        b"app.get(ROUTES.users, handler);\n"
    )

    record = _parse_typescript(source, "app.ts")
    routes = [statement for statement in record.statements if statement.semanticType == "route"]

    assert [(route.method, route.endpoint, route.isRegex) for route in routes] == [
        ("GET", None, False)
    ]
    assert routes[0].framework == "fastify"
    assert record.framework == "fastify"


def test_unresolved_route_object_url_is_captured_with_null_endpoint() -> None:
    source = (
        b"const app = Fastify();\n"
        b"app.route({ method: 'GET', url: ROUTES.users, handler });\n"
    )

    record = _parse_typescript(source, "app.ts")
    routes = [statement for statement in record.statements if statement.semanticType == "route"]

    assert [(route.method, route.endpoint, route.isRegex) for route in routes] == [
        ("GET", None, False)
    ]
    assert routes[0].framework == "fastify"
    assert record.framework == "fastify"


def test_template_route_paths_use_parameter_placeholders() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.get(`/users/${userId}/posts/${user.id}`, handler);"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint, route.semanticType) for route in routes] == [
        ("GET", "/users/{userId}/posts/{id}", "route")
    ]


def test_template_route_path_resolves_constants_and_falls_back_to_placeholders() -> None:
    source = (
        b"const BASE = '/api';\n"
        b"const app = Fastify();\n"
        b"app.get(`${BASE}/users`, handler);\n"
        b"app.get(`${REPO_BASE}/admin`, handler);\n"
        b"app.get(`${DYNAMIC_BASE}/public`, handler);"
    )

    resolution_index = TsAliasIndex(base_dir=".", paths={}, const_values={"REPO_BASE": "/internal"})
    routes = _detect_routes(source, resolution_index=resolution_index)

    assert [(route.method, route.endpoint) for route in routes] == [
        ("GET", "/api/users"),
        ("GET", "/internal/admin"),
        ("GET", "/public"),
    ]


def test_template_route_with_constant_base_is_resolved() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const f = Fastify();\n"
        b"const BASE = '/api';\n"
        b"f.get(`${BASE}/users`, handler);\n"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/api/users")]


def test_handler_inside_route_options_is_captured() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const f = Fastify();\n"
        b"f.get('/opts', { schema: {}, handler: getUsers });\n"
    )

    routes = _detect_routes(source)

    assert [(route.endpoint, route.handler) for route in routes] == [("/opts", "getUsers")]

    record = _parse_typescript(source, "opts.ts")
    captured = [statement for statement in record.statements if statement.framework == "fastify"]
    assert [(route.endpoint, route.handler) for route in captured] == [("/opts", "getUsers")]


def test_route_hooks_are_captured_as_guards_but_auth_only_when_auth_like() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.get('/account', { preHandler: requireAuth }, getAccount);\n"
        b"fastify.route({ method: 'POST', url: '/admin', "
        b"onRequest: [authenticate, Admin.authorize], "
        b"preValidation: validateRequest, handler: saveAdmin });\n"
        b"fastify.get('/health', { preValidation: validateRequest }, health);\n"
        b"fastify.get('/author', { preHandler: authorsLoader }, authorHandler);\n"
        b"fastify.get('/oauth-log', { preHandler: oauthRedirectLogger }, oauthHandler);\n"
        b"fastify.get('/session-log', { preHandler: sessionLogger }, sessionHandler);\n"
        b"fastify.get('/login-limit', { preHandler: loginRateLimit }, loginHandler);"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint, route.guards, route.authRequired) for route in routes] == [
        ("GET", "/account", ["requireAuth"], True),
        (
            "POST",
            "/admin",
            ["authenticate", "Admin.authorize", "validateRequest"],
            True,
        ),
        ("GET", "/health", ["validateRequest"], None),
        ("GET", "/author", ["authorsLoader"], None),
        ("GET", "/oauth-log", ["oauthRedirectLogger"], None),
        ("GET", "/session-log", ["sessionLogger"], None),
        ("GET", "/login-limit", ["loginRateLimit"], None),
    ]


def test_detects_route_after_later_fastify_reassignment() -> None:
    source = (
        b"let app;\n"
        b"app = Fastify();\n"
        b"app.get('/late', handler);"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/late")]


def test_detects_route_with_named_handler() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const fastify = Fastify();\n"
        b"function getUsers(request, reply) { return { users: [] }; }\n"
        b"fastify.get('/users', getUsers);"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint, route.handler) for route in routes] == [
        ("GET", "/users", "getUsers")
    ]


def test_shorthand_route_resolves_handler_from_options_object() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"function getUsers(request, reply) { return []; }\n"
        b"fastify.get('/opts', { schema: {}, handler: getUsers });"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint, route.handler) for route in routes] == [
        ("GET", "/opts", "getUsers")
    ]


def test_inline_handler_route_is_parented_to_enclosing_plugin_function() -> None:
    source = (
        b'import type { FastifyInstance } from "fastify";\n'
        b"export async function userRoutes(fastify: FastifyInstance) {\n"
        b"  fastify.get('/profile', async (req, rep) => ({ profile: true }));\n"
        b"}"
    )

    record = _parse_typescript(source, "routes/users.ts")
    route = next(statement for statement in record.statements if statement.framework == "fastify")
    plugin = next(function for function in record.functions if function.name == "userRoutes")

    assert route.handler is None
    assert route.parentId == plugin.id


def test_fastify_routes_from_exported_plugin_bindings() -> None:
    sources = [
        b"import type { FastifyPluginAsync } from 'fastify'; "
        b"const root: FastifyPluginAsync = async (fastify, opts) => { "
        b"fastify.get('/', h); }; export default root",
        b"import type { FastifyPluginAsync } from 'fastify'; "
        b"const users: FastifyPluginAsync = async (app) => { "
        b"app.get('/users', h); }",
        b"import type { FastifyPluginCallback } from 'fastify'; "
        b"const plugin: FastifyPluginCallback = (server, opts, done) => { "
        b"server.get('/cb', h); done(); }",
        b"module.exports = async function (fastify, opts) { fastify.get('/cjs', h); }",
        b"module.exports = fp(async function (fastify) { fastify.get('/wrapped-cjs', h); })",
        b"import type { FastifyPluginAsync } from 'fastify'; "
        b"const users: FastifyPluginAsync = async (fastify) => { "
        b"fastify.get('/users', h); }; export default fp(users)",
    ]
    expected_endpoints = [
        ["/"],
        ["/users"],
        ["/cb"],
        ["/cjs"],
        ["/wrapped-cjs"],
        ["/users"],
    ]

    for index, (source, expected) in enumerate(zip(sources, expected_endpoints)):
        extension = "js" if index in (3, 4) else "ts"
        record = _parse_typescript(source, f"plugin-{index}.{extension}")
        routes = [statement for statement in record.statements if statement.framework == "fastify"]

        assert [route.endpoint for route in routes] == expected


def test_fastify_plugin_shapes_through_registry_selection() -> None:
    sources = [
        (
            "root.ts",
            b"import type { FastifyPluginAsync } from 'fastify'; "
            b"const root: FastifyPluginAsync = async (fastify, opts) => { "
            b"fastify.get('/', h); }; export default root",
            [("GET", "/")],
        ),
        (
            "users.ts",
            b"import type { FastifyPluginAsync } from 'fastify'; "
            b"const users: FastifyPluginAsync = async (app) => { "
            b"app.get('/users', h); }",
            [("GET", "/users")],
        ),
        (
            "callback.ts",
            b"import type { FastifyPluginCallback } from 'fastify'; "
            b"const plugin: FastifyPluginCallback = (server, opts, done) => { "
            b"server.get('/cb', h); done(); }",
            [("GET", "/cb")],
        ),
        (
            "plugin.cjs",
            b"module.exports = async function (fastify, opts) { fastify.get('/cjs', h) }",
            [("GET", "/cjs")],
        ),
        (
            "users.ts",
            b"import type { FastifyPluginAsync } from 'fastify'; "
            b"const users: FastifyPluginAsync = async (fastify) => { "
            b"fastify.get('/users', h); }; export default fp(users)",
            [("GET", "/users")],
        ),
        (
            "routes.ts",
            b"import type { FastifyInstance } from 'fastify'; "
            b"export default async function routes(fastify: FastifyInstance) { "
            b"fastify.get('/direct', h); }",
            [("GET", "/direct")],
        ),
    ]

    registry.clear()
    try:
        registry.discover_builtin()
        for path, source, expected in sources:
            parser = registry.select(path, source)
            assert parser is not None
            assert parser.name == "typescript"

            record = parser.parse_file(
                ParseContext(
                    path=path,
                    abs_path=Path(path),
                    source=source,
                    repo_root=Path("."),
                    capture_statements=True,
                )
            )

            routes = [
                statement for statement in record.statements if statement.framework == "fastify"
            ]
            assert [(route.method, route.endpoint) for route in routes] == expected
    finally:
        registry.clear()


def test_fastify_register_prefix_uses_repository_constant_index() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const app = Fastify();\n"
        b"app.register(async function users(fastify) {\n"
        b"  fastify.get('/', handler);\n"
        b"}, { prefix: API_PREFIX });\n"
    )
    resolution_index = TsAliasIndex(base_dir=".", paths={}, const_values={"API_PREFIX": "/api"})

    record = TypeScriptParser().parse_file(
        ParseContext(
            path="server.ts",
            abs_path=Path("server.ts"),
            source=source,
            repo_root=Path("."),
            capture_statements=True,
            resolution_index=resolution_index,
        )
    )
    routes = [statement for statement in record.statements if statement.framework == "fastify"]

    assert [route.endpoint for route in routes] == ["/api", "/api"]


def test_fastify_routes_are_additive_to_react_parser() -> None:
    source = (
        b'import next from "next";\n'
        b'import React from "react";\n'
        b'import Fastify from "fastify";\n'
        b"const f = Fastify();\n"
        b"f.get('/api/x', handler);\n"
    )
    registry.clear()
    try:
        registry.discover_builtin()
        parser = registry.select("server.ts", source)
        assert parser is not None
        assert parser.name == "typescript-react"

        record = parser.parse_file(
            ParseContext(
                path="server.ts",
                abs_path=Path("server.ts"),
                source=source,
                repo_root=Path("."),
                capture_statements=True,
            )
        )

        fastify_routes = [
            statement for statement in record.statements if statement.framework == "fastify"
        ]
        assert [route.endpoint for route in fastify_routes] == ["/api/x"]
    finally:
        registry.clear()


def test_fastify_routes_are_additive_to_angular_parser() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b'import { Component } from "@angular/core";\n'
        b"const f = Fastify();\n"
        b"f.get('/api/x', handler);\n"
        b"@Component({ selector: 'app-root' }) class App {}\n"
    )
    registry.clear()
    try:
        registry.discover_builtin()
        parser = registry.select("app.ts", source)
        assert parser is not None
        assert parser.name == "typescript-angular"

        record = parser.parse_file(
            ParseContext(
                path="app.ts",
                abs_path=Path("app.ts"),
                source=source,
                repo_root=Path("."),
                capture_statements=True,
            )
        )

        fastify_routes = [
            statement for statement in record.statements if statement.framework == "fastify"
        ]
        assert [route.endpoint for route in fastify_routes] == ["/api/x"]
    finally:
        registry.clear()


def test_fastify_route_detection_requires_statement_capture() -> None:
    source = b"import Fastify from \"fastify\";\nconst f = Fastify(); f.get('/api/x', handler);"

    record = _parse_typescript(source, "server.ts", capture_statements=False)

    assert not any(statement.framework == "fastify" for statement in record.statements)


def test_fastify_plugin_names_do_not_leak_to_sibling_functions() -> None:
    source = (
        b"export function plugin(fastify) {}\n"
        b"function unrelated() { fastify.get('/wrong', handler); }"
    )
    root = parse_source("typescript", source).root_node
    record = FileRecord(id="app.ts", path="app.ts", type="code", language="typescript", loc=1)

    assert detect_fastify_routes(root, source, "app.ts", record, seen_ids=set()) == []


def test_nested_helper_in_exported_non_plugin_is_not_a_plugin() -> None:
    source = (
        b"export function ordinary() { "
        b"function helper(fastify) { fastify.get('/wrong', handler); } }"
    )
    root = parse_source("typescript", source).root_node
    record = FileRecord(id="app.ts", path="app.ts", type="code", language="typescript", loc=1)

    assert detect_fastify_routes(root, source, "app.ts", record, seen_ids=set()) == []


def test_fastify_instance_bindings_respect_block_and_parameter_shadowing() -> None:
    source = (
        b"const app = Fastify();\n"
        b"{ const app = axios; app.get('/block-shadow', handler); }\n"
        b"function unrelated(app) { app.get('/parameter-shadow', handler); }\n"
        b"app.get('/outer', handler);"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/outer")]


def test_block_local_fastify_instance_does_not_escape_its_scope() -> None:
    source = (
        b"{ const local = Fastify(); local.get('/inside', handler); }\n"
        b"local.get('/outside', handler);"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/inside")]


def test_fastify_instance_is_visible_from_nested_closure() -> None:
    source = b"const app = Fastify();\nfunction registerRoutes() { app.get('/captured', handler); }"

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/captured")]


def test_fastify_instance_binding_is_not_visible_before_its_initializer() -> None:
    source = b"app.get('/before', handler);\nconst app = Fastify();\napp.get('/after', handler);"

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/after")]


def test_route_path_constants_respect_scope_shadowing() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"const URL = '/outer';\n"
        b"fastify.get(URL, handler);\n"
        b"{ const URL = '/inner'; fastify.get(URL, handler); }"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [
        ("GET", "/outer"),
        ("GET", "/inner"),
    ]


def test_route_path_constant_before_declaration_is_captured_as_unresolved() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.get(URL, handler);\n"
        b"const URL = '/late';\n"
        b"fastify.get(URL, handler);"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [
        ("GET", None),
        ("GET", "/late"),
    ]


def test_many_route_path_constants_resolve_from_the_per_file_index() -> None:
    count = 300
    declarations = b"\n".join(
        f"const PATH_{index} = '/route-{index}';".encode() for index in range(count)
    )
    routes_source = b"\n".join(
        f"fastify.get(PATH_{index}, handler);".encode() for index in range(count)
    )
    source = b"const fastify = Fastify();\n" + declarations + b"\n" + routes_source
    root = parse_source("typescript", source).root_node
    index = fastify_routes._FileIndex(root, source, {})
    pending = [root]
    scope_lookup_node_count = 0
    while pending:
        node = pending.pop()
        if node.type in {
            "call_expression",
            "identifier",
            "shorthand_property_identifier",
            "shorthand_property_identifier_pattern",
        }:
            scope_lookup_node_count += 1
        pending.extend(node.named_children)

    routes = _detect_routes(source)

    assert len(routes) == count
    assert routes[0].endpoint == "/route-0"
    assert routes[-1].endpoint == f"/route-{count - 1}"
    assert len(index.node_scopes) == scope_lookup_node_count


def test_route_method_array_keeps_unresolved_method_null() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.route({ method: [unknownMethod, 'get'], url: '/mixed', handler });"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [
        ("GET", "/mixed"),
        (None, "/mixed"),
    ]


def test_all_shorthand_registers_every_method() -> None:
    source = b"const fastify = Fastify(); fastify.all('/all', handler);"

    routes = _detect_routes(source)

    assert [route.method for route in routes] == [
        "GET",
        "POST",
        "PUT",
        "DELETE",
        "PATCH",
        "OPTIONS",
        "HEAD",
        "TRACE",
    ]
    assert {route.endpoint for route in routes} == {"/all"}


def test_nested_register_prefixes_are_joined() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.register(async function outer(outerServer) {\n"
        b"  outerServer.register(async function inner(innerServer) {\n"
        b"    innerServer.get('/items', handler);\n"
        b"  }, { prefix: '/v2' });\n"
        b"}, { prefix: '/api' });"
    )

    routes = [route for route in _detect_routes(source) if route.method is not None]

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/api/v2/items")]


def test_register_resolves_named_plugin_references_with_prefixes() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"const users = async (usersServer) => {\n"
        b"  usersServer.get('/users', handler);\n"
        b"};\n"
        b"function admin(fastify) {\n"
        b"  fastify.get('/admin', handler);\n"
        b"}\n"
        b"fastify.register(users, { prefix: '/api' });\n"
        b"fastify.register(admin, { prefix: '/internal' });\n"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [
        (None, "/api"),
        ("GET", "/api/users"),
        (None, "/internal"),
        ("GET", "/internal/admin"),
    ]


def test_nested_named_plugins_keep_prefixes_without_duplicate_routes() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"async function users(usersServer) {\n"
        b"  usersServer.get('/items', handler);\n"
        b"}\n"
        b"async function api(apiServer) {\n"
        b"  apiServer.register(users, { prefix: '/users' });\n"
        b"}\n"
        b"fastify.register(api, { prefix: '/api' });"
    )

    routes = [route for route in _detect_routes(source) if route.method is not None]

    assert [(route.method, route.endpoint) for route in routes] == [
        ("GET", "/api/users/items")
    ]


def test_unresolved_register_prefix_is_null_on_mount_and_nested_route() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.register(async function plugin(pluginServer) {\n"
        b"  pluginServer.get('/items', handler);\n"
        b"}, { prefix: dynamicPrefix });"
    )

    routes = [route for route in _detect_routes(source) if route.method is not None]

    assert [(route.method, route.endpoint) for route in routes] == [("GET", None)]

    mounts = [route for route in _detect_routes(source) if route.routeKind == "mount"]
    assert len(mounts) == 1
    assert mounts[0].endpoint is None


def test_unresolved_route_handler_is_null() -> None:
    routes = _detect_routes(b"const fastify = Fastify(); fastify.get('/health', {});")

    assert len(routes) == 1
    assert routes[0].handler is None
    assert routes[0].handlerLine is None


def test_fastify_records_validate_against_capture_schema() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.get('/health', {});\n"
        b"fastify.register(async function plugin(server) {\n"
        b"  server.get('/items', handler);\n"
        b"}, { prefix: dynamicPrefix });"
    )
    record = _parse_typescript(source, "server.ts")

    errors = list(
        Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
            json.loads(to_line(record))
        )
    )
    assert not errors, errors


def test_iterative_walk_detects_routes_beyond_previous_depth_cutoff() -> None:
    nested_blocks = b"if (true) {" * 950
    closing_blocks = b"}" * 950
    source = (
        b"const fastify = Fastify();\n"
        + nested_blocks
        + b"fastify.get('/deep', handler);"
        + closing_blocks
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/deep")]


def test_shorthand_route_accepts_schema_options_before_handler() -> None:
    source = b"const fastify = Fastify();\nfastify.get('/s', { schema: {} }, handler);"

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint, route.handler) for route in routes] == [
        ("GET", "/s", "handler")
    ]


def test_self_registering_plugin_terminates() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"async function plugin(app) {\n"
        b"  app.get('/x', handler);\n"
        b"  app.register(plugin, { prefix: '/n' });\n"
        b"}\n"
        b"fastify.register(plugin, { prefix: '/a' });"
    )

    routes = _detect_routes(source)

    assert ("GET", "/a/x") in [(route.method, route.endpoint) for route in routes]


def test_mutually_registering_plugins_terminate() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"async function a(app) {\n"
        b"  app.get('/a', handler);\n"
        b"  app.register(b, { prefix: '/b' });\n"
        b"}\n"
        b"async function b(app) {\n"
        b"  app.get('/b', handler);\n"
        b"  app.register(a, { prefix: '/a' });\n"
        b"}\n"
        b"fastify.register(a);"
    )

    routes = _detect_routes(source)

    endpoints = [(route.method, route.endpoint) for route in routes]
    assert ("GET", "/a") in endpoints
    assert (None, "/b") in endpoints


def test_plugin_declared_after_register_is_resolved() -> None:
    source = (
        b"const f = Fastify();\n"
        b"f.register(users, { prefix: '/v1' });\n"
        b"async function users(i) { i.get('/u', h); }"
    )

    routes = _detect_routes(source)

    assert ("GET", "/v1/u") in [(route.method, route.endpoint) for route in routes]


def test_fastify_plugin_wrapper_and_wildcard_routes() -> None:
    source = (
        b"const f = Fastify();\n"
        b"f.register(fp(users), { prefix: '/v1' });\n"
        b"f.get('*', h);\n"
        b"async function users(i) { i.get('/u', h); i.get('', h); }"
    )

    endpoints = [(r.method, r.endpoint) for r in _detect_routes(source)]

    assert ("GET", "/u") in endpoints
    assert (None, "/") in endpoints
    assert ("GET", "/*") in endpoints


def test_fastify_plugin_wrapper_ignores_prefix_but_plain_plugin_uses_it() -> None:
    wrapped_source = (
        b"const f = Fastify();\n"
        b"f.register(fp(async (i) => { i.get('/health', h); }), { prefix: '/api' });"
    )
    plain_source = (
        b"const f = Fastify();\n"
        b"f.register(async (i) => { i.get('/health', h); }, { prefix: '/api' });"
    )

    wrapped_routes = _detect_routes(wrapped_source)
    plain_routes = _detect_routes(plain_source)

    assert [(route.method, route.endpoint) for route in wrapped_routes] == [
        (None, "/"),
        ("GET", "/health"),
    ]
    assert [(route.method, route.endpoint) for route in plain_routes] == [
        (None, "/api"),
        ("GET", "/api/health"),
    ]


def test_chained_register_and_route_calls_are_detected() -> None:
    source = (
        b"const f = Fastify();\n"
        b"f.register(a, { prefix: '/a' }).register(b, { prefix: '/b' }).get('/c', h);\n"
        b"async function a(i) { i.get('/x', h); }\n"
        b"async function b(i) { i.get('/y', h); }"
    )

    endpoints = {(r.method, r.endpoint) for r in _detect_routes(source)}

    assert {("GET", "/a/x"), ("GET", "/b/y"), ("GET", "/c"), (None, "/a"), (None, "/b")} <= endpoints
