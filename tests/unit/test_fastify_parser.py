"""Regression tests for TypeScript Fastify claims, route guards, and priority."""

from __future__ import annotations

from breezeai_cog.core import registry
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.parsers.typescript_fastify.parser import FastifyParser
from breezeai_cog.parsers.typescript_fastify.routes import (
    _add_fastify_instance_name,
    detect_fastify_routes,
    _dispatch_call,
)
from breezeai_cog.schemas import FileRecord


def _first_call(source: bytes):
    def walk(node):
        if node.type == "call_expression":
            return node
        for child in node.children:
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(parse_source("typescript", source).root_node)


def test_fastify_claims_only_runtime_imports() -> None:
    parser = FastifyParser()

    assert parser.claims("app.ts", b'import Fastify from "fastify";')
    assert parser.claims("app.ts", b'const Fastify = require("fastify");')
    assert not parser.claims("app.ts", b'const express = require("express");')
    assert not parser.claims("app.ts", b'// from "fastify"')
    assert not parser.claims("app.ts", b'const help = \'from "fastify"\';')
    assert not parser.claims(
        "app.ts", b'import type { FastifyInstance } from "fastify";'
    )


def test_non_fastify_exported_handlers_do_not_leak_instance_names() -> None:
    source = (
        b"export async function handleUsers(req: Request, res: Response) {}"
    )
    names: set[str] = set()
    root = parse_source("typescript", source).root_node

    for child in root.children:
        _add_fastify_instance_name(child, source, names)

    assert names == set()


def test_fastify_plugin_names_do_not_leak_to_sibling_functions() -> None:
    source = (
        b"export function plugin(fastify) {}\n"
        b"function unrelated() { fastify.get('/wrong', handler); }"
    )
    root = parse_source("typescript", source).root_node
    record = FileRecord(id="app.ts", path="app.ts", type="code", language="typescript", loc=1)

    assert detect_fastify_routes(root, source, "app.ts", record, seen_ids=set()) == []


def test_near_match_fastify_type_does_not_claim_plugin() -> None:
    source = b"export function handler(req: NotFastifyInstance) { req.get('/wrong', handler); }"
    names: set[str] = set()
    root = parse_source("typescript", source).root_node

    for child in root.children:
        _add_fastify_instance_name(child, source, names)

    assert names == set()


def test_route_and_register_require_fastify_identifier_receiver() -> None:
    assert _dispatch_call(
        _first_call(b"router.route('/users').get(handler)"),
        b"router.route('/users').get(handler)",
        {"fastify"},
    ) is None
    assert _dispatch_call(
        _first_call(b"container.register('UserService', {})"),
        b"container.register('UserService', {})",
        {"fastify"},
    ) is None
    assert _dispatch_call(
        _first_call(b"this.router.route('/path', handler)"),
        b"this.router.route('/path', handler)",
        {"fastify"},
    ) is None

    source = b"fastify.route({ url: '/', handler })"
    assert _dispatch_call(_first_call(source), source, {"fastify"})[0] == "app_route"
    source = b"fastify.register(plugin)"
    assert _dispatch_call(_first_call(source), source, {"fastify"})[0] == "register"


def test_fastify_priority_does_not_displace_angular() -> None:
    registry.clear()
    try:
        registry.discover_builtin()
        source = (
            b'import Fastify from "fastify";\n'
            b'import { Component } from "@angular/core";\n'
        )
        assert registry.select("app.ts", source).name == "typescript-angular"
    finally:
        registry.clear()
