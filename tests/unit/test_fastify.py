"""Regression tests for additive TypeScript Fastify route detection."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.core import registry
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.parsers.typescript.imports import TsAliasIndex
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.parsers.typescript_fastify.routes import detect_fastify_routes
from breezeai_cog.schemas import FileRecord


def _parse_typescript(
    source: bytes,
    path: str,
    *,
    capture_statements: bool = True,
    resolution_index=None,
):
    context = ParseContext(
        path=path,
        abs_path=Path(path),
        source=source,
        repo_root=Path("."),
        capture_statements=capture_statements,
        resolution_index=resolution_index,
    )
    return TypeScriptParser().parse_file(context)


def _detect_routes(source: bytes, path: str = "app.ts"):
    root = parse_source("typescript", source).root_node
    record = FileRecord(
        id=path, path=path, type="code", language="typescript", loc=1
    )
    return detect_fastify_routes(root, source, path, record, seen_ids=set())


def test_detects_route_from_known_fastify_instance() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const fastify = Fastify();\n"
        b"fastify.get('/users', async (request, reply) => ({ users: [] }));"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/users")]
    assert routes[0].framework == "fastify"


def test_ignores_unrelated_get_method() -> None:
    source = (
        b'import axios from "axios";\n'
        b"const client = axios;\n"
        b"client.get('/users', async () => []);"
    )

    assert _detect_routes(source) == []


def test_detects_post_route() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const fastify = Fastify();\n"
        b"fastify.post('/users', async (request, reply) => ({ created: true }));"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("POST", "/users")]


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


def test_fastify_routes_from_exported_plugin_bindings() -> None:
    sources = [
        b"import type { FastifyPluginAsync } from 'fastify'; "
        b"const root: FastifyPluginAsync = async (fastify, opts) => { "
        b"fastify.get('/', h); }; export default root",
        b"module.exports = async function (fastify, opts) { "
        b"fastify.get('/cjs', h); }",
        b"module.exports = fp(async function (fastify) { "
        b"fastify.get('/wrapped-cjs', h); })",
        b"import type { FastifyPluginAsync } from 'fastify'; "
        b"const users: FastifyPluginAsync = async (fastify) => { "
        b"fastify.get('/users', h); }; export default fp(users)",
    ]
    expected_endpoints = [["/"], ["/cjs"], ["/wrapped-cjs"], ["/users"]]

    for index, (source, expected) in enumerate(zip(sources, expected_endpoints)):
        extension = "js" if index in (1, 2) else "ts"
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
            "plugin.cjs",
            b"module.exports = async function (fastify, opts) { "
            b"fastify.get('/cjs', h) }",
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
                statement
                for statement in record.statements
                if statement.framework == "fastify"
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
    resolution_index = TsAliasIndex(
        base_dir=".", paths={}, const_values={"API_PREFIX": "/api"}
    )

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
            statement
            for statement in record.statements
            if statement.framework == "fastify"
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
            statement
            for statement in record.statements
            if statement.framework == "fastify"
        ]
        assert [route.endpoint for route in fastify_routes] == ["/api/x"]
    finally:
        registry.clear()


def test_fastify_route_detection_requires_statement_capture() -> None:
    source = (
        b'import Fastify from "fastify";\n'
        b"const f = Fastify(); f.get('/api/x', handler);"
    )

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
    record = FileRecord(
        id="app.ts", path="app.ts", type="code", language="typescript", loc=1
    )

    assert detect_fastify_routes(root, source, "app.ts", record, seen_ids=set()) == []


def test_route_method_array_keeps_unresolved_method_as_unknown() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.route({ method: [unknownMethod, 'get'], url: '/mixed', handler });"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [
        ("GET", "/mixed"),
        ("UNKNOWN", "/mixed"),
    ]


def test_all_shorthand_registers_every_method() -> None:
    source = b"const fastify = Fastify(); fastify.all('/all', handler);"

    routes = _detect_routes(source)

    assert [route.method for route in routes] == [
        "GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"
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

    assert [(route.method, route.endpoint) for route in routes] == [
        ("GET", "/api/v2/items")
    ]


def test_unresolved_register_prefix_is_marked_on_nested_route() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.register(async function plugin(pluginServer) {\n"
        b"  pluginServer.get('/items', handler);\n"
        b"}, { prefix: dynamicPrefix });"
    )

    routes = [route for route in _detect_routes(source) if route.method is not None]

    assert [(route.method, route.endpoint) for route in routes] == [
        ("GET", "/<unresolved-prefix>/items")
    ]


def test_shorthand_route_accepts_schema_options_before_handler() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.get('/s', { schema: {} }, handler);"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint, route.handler) for route in routes] == [
        ("GET", "/s", "handler")
    ]


