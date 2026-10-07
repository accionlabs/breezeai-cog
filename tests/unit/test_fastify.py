"""Regression tests for additive TypeScript Fastify route detection."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.parsers.typescript.imports import TsAliasIndex
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.parsers.typescript_fastify import routes as fastify_routes
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
    record = FileRecord(id=path, path=path, type="code", language="typescript", loc=1)
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


def test_route_path_constant_is_not_resolved_from_a_later_declaration() -> None:
    source = (
        b"const fastify = Fastify();\n"
        b"fastify.get(URL, handler);\n"
        b"const URL = '/late';\n"
        b"fastify.get(URL, handler);"
    )

    routes = _detect_routes(source)

    assert [(route.method, route.endpoint) for route in routes] == [("GET", "/late")]


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
    syntax_node_count = 0
    scope_lookup_node_count = 0
    while pending:
        node = pending.pop()
        syntax_node_count += 1
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
    assert index.indexed_node_count == syntax_node_count
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
