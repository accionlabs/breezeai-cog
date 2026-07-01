"""NestJS framework parser: route detection, parentId linkage, base reuse, override."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_nestjs.parser import NestJSParser
from breezeai_cog.schemas import FileRecord

SRC = b'''import { Controller, Get, Post, Param } from '@nestjs/common';

@Controller('orders')
export class OrderController {
  @Get(':id')
  getOne(@Param('id') id: string) { return id; }

  @Post()
  create() {}

  helper() { return 1; }
}
'''


def _parse(tmp_path, *, capture=True) -> FileRecord:
    p = tmp_path / "order.controller.ts"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="order.controller.ts", abs_path=p, source=SRC, repo_root=tmp_path,
                       capture_statements=capture)
    return NestJSParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with --capture-statements (spec A4).
    rec = _parse(tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_routes_detected_and_linked(tmp_path) -> None:
    rec = _parse(tmp_path)
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {"/orders/:id", "/orders"}
    assert routes["/orders/:id"].method == "GET" and routes["/orders/:id"].handler == "getOne"
    assert routes["/orders/:id"].framework == "nestjs"
    assert routes["/orders"].method == "POST" and routes["/orders"].handler == "create"
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes.values())  # parented to handler methods
    assert rec.framework == "nestjs"


def test_base_extraction_reused(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert {f.name for f in rec.functions} == {"getOne", "create", "helper"}
    assert rec.language == "typescript"


def test_param_decorators_captured(tmp_path) -> None:
    # spec C4.1 — TS parameter decorators (@Param/@Body/…) now captured.
    rec = _parse(tmp_path)
    getone = next(f for f in rec.functions if f.name == "getOne")
    assert [d.name for d in getone.params[0].decorators] == ["Param"]


_ATTRS_SRC = b'''import { Controller, Post, Body, UseGuards } from '@nestjs/common';
import { ApiResponse } from '@nestjs/swagger';

@Controller('orders')
@UseGuards(JwtAuthGuard)
export class OrderController {
  @Post()
  @UseGuards(RolesGuard)
  @ApiResponse({ status: 201, type: OrderDto })
  create(@Body() dto: CreateOrderDto) { return dto; }

  @Get()
  list() { return []; }
}
'''


def test_route_attributes(tmp_path) -> None:
    # spec C5 — guards (controller+method merged), authRequired, requestDTO, responseDTO.
    p = tmp_path / "attrs.controller.ts"
    p.write_text(_ATTRS_SRC.decode())
    ctx = ParseContext(path="attrs.controller.ts", abs_path=p, source=_ATTRS_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = NestJSParser().parse_file(ctx)
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    create = routes["create"]
    assert create.guards == ["JwtAuthGuard", "RolesGuard"]  # controller + method merged
    assert create.authRequired is True
    assert create.requestDTO == "CreateOrderDto"
    assert create.responseDTO == "OrderDto"
    assert create.isRegex is False
    # controller guard still applies to routes with no method-level guard
    assert routes["list"].guards == ["JwtAuthGuard"] and routes["list"].authRequired is True
    assert routes["list"].requestDTO is None and routes["list"].responseDTO is None


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_claims_selects_nestjs() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(NestJSParser())
    assert registry.select("x.ts", b"import { Controller } from '@nestjs/common';").name == "typescript-nestjs"
    assert registry.select("x.ts", b"const x = 1;").name == "typescript"  # plain TS -> base
    registry.clear()
