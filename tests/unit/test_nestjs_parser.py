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


def _parse(tmp_path) -> FileRecord:
    p = tmp_path / "order.controller.ts"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="order.controller.ts", abs_path=p, source=SRC, repo_root=tmp_path)
    return NestJSParser().parse_file(ctx)


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
