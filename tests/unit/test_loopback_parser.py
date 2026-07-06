"""LoopBack framework parser: route detection, parentId linkage, base reuse, override."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript_loopback.parser import LoopBackParser
from breezeai_cog.schemas import FileRecord

SRC = b'''import {get, post, del, param, requestBody, operation, api} from '@loopback/rest';
import {repository} from '@loopback/repository';

@api({basePath: '/products'})
export class ProductController {
  constructor(@repository(ProductRepository) public repo: ProductRepository) {}

  @get('/{id}')
  async findById(@param.path.number('id') id: number): Promise<Product> {
    return this.repo.findById(id);
  }

  @post('/')
  async create(@requestBody() product: Product): Promise<Product> {
    return this.repo.create(product);
  }

  @del('/{id}')
  async deleteById(@param.path.number('id') id: number): Promise<void> {
    await this.repo.deleteById(id);
  }

  @operation('patch', '/{id}')
  async updateById(): Promise<void> {}

  helper() { return 1; }
}
'''


def _parse(tmp_path, *, capture=True) -> FileRecord:
    p = tmp_path / "product.controller.ts"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="product.controller.ts", abs_path=p, source=SRC, repo_root=tmp_path,
                       capture_statements=capture)
    return LoopBackParser().parse_file(ctx)


def test_routes_require_capture_statements(tmp_path) -> None:
    # Routes are statements — only emitted with --capture-statements (spec A4).
    rec = _parse(tmp_path, capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_routes_detected_and_linked(tmp_path) -> None:
    rec = _parse(tmp_path)
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert set(routes) == {
        ("GET", "/products/{id}"),
        ("POST", "/products"),
        ("DELETE", "/products/{id}"),
        ("PATCH", "/products/{id}"),
    }
    assert routes[("GET", "/products/{id}")].handler == "findById"
    assert routes[("DELETE", "/products/{id}")].handler == "deleteById"
    assert routes[("PATCH", "/products/{id}")].handler == "updateById"  # @operation generic verb
    assert all(r.framework == "loopback" for r in routes.values())
    fn_ids = {f.id for f in rec.functions}
    assert all(r.parentId in fn_ids for r in routes.values())  # parented to handler methods
    assert rec.framework == "loopback"


def test_base_extraction_reused(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert {"findById", "create", "deleteById", "updateById", "helper"} <= {f.name for f in rec.functions}
    assert rec.language == "typescript"


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_comment_between_decorator_and_handler(tmp_path) -> None:
    # Regression: a comment (commented-out old signature / eslint-disable) between the
    # route decorator and its handler method must not drop the route (real LoopBack repos
    # hit this; the NestJS parser already guarded it).
    src = (
        b"import {get, param} from '@loopback/rest';\n"
        b"export class TimezoneController {\n"
        b"  @get('/timezones', {responses: {}})\n"
        b"  // async find(): Promise<object> {\n"
        b"  async find(@param.filter(Timezone) filter?: Filter<Timezone>): Promise<object> {\n"
        b"    return this.svc.find(filter);\n"
        b"  }\n"
        b"  @get('/timezones/{id}')\n"
        b"  // eslint-disable-next-line @typescript-eslint/no-explicit-any\n"
        b"  async findById(): Promise<any> { return {}; }\n"
        b"}\n"
    )
    p = tmp_path / "timezone.controller.ts"
    p.write_text(src.decode())
    ctx = ParseContext(path="timezone.controller.ts", abs_path=p, source=src,
                       repo_root=tmp_path, capture_statements=True)
    rec = LoopBackParser().parse_file(ctx)
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert routes[("GET", "/timezones")].handler == "find"
    assert routes[("GET", "/timezones/{id}")].handler == "findById"


def test_computed_path_rendered_wellformed(tmp_path) -> None:
    # R1(a): a route path built by concatenation / template literal must render as a
    # well-formed `{placeholder}` path, not a malformed `+`-spliced string.
    src = (
        b"import {get, post} from '@loopback/rest';\n"
        b"import appConfig from './config';\n"
        b"export class C {\n"
        b"  @get(appConfig.apiPathV2 + '/tender-status/count')\n"
        b"  async count() { return 0; }\n"
        b"  @post(`${appConfig.apiPathV2}/subscriber-setting`)\n"
        b"  async setting() {}\n"
        b"  @get('/api/v2/plain/literal')\n"
        b"  async plain() {}\n"
        b"}\n"
    )
    p = tmp_path / "c.controller.ts"
    p.write_text(src.decode())
    ctx = ParseContext(path="c.controller.ts", abs_path=p, source=src,
                       repo_root=tmp_path, capture_statements=True)
    eps = {s.handler: s.endpoint for s in LoopBackParser().parse_file(ctx).statements
           if s.semanticType == "route"}
    # concat: no '+' and no dangling quote — a clean placeholder for the config var
    assert "+" not in eps["count"] and "'" not in eps["count"]
    assert eps["count"].endswith("/tender-status/count") and "{apiPathV2}" in eps["count"]
    # template literal: ${...} normalized to {...}
    assert eps["setting"] == "/{apiPathV2}/subscriber-setting"
    # plain literal unchanged
    assert eps["plain"] == "/api/v2/plain/literal"


def test_request_and_response_dto(tmp_path) -> None:
    # R2: capture requestDTO from the @requestBody() param type and responseDTO from the
    # handler return type.
    src = (
        b"import {get, post, requestBody} from '@loopback/rest';\n"
        b"export class C {\n"
        b"  @post('/clean')\n"
        b"  async clean(@requestBody() dto: CleanDataDTO): Promise<object> { return {}; }\n"
        b"  @get('/timezones')\n"
        b"  async find(): Promise<ResponseApi<Timezone[]>> { return null as any; }\n"
        b"  @post('/inline')\n"
        b"  async inline(@requestBody() body: {a: number; b: string}): Promise<void> {}\n"
        b"}\n"
    )
    p = tmp_path / "c.controller.ts"
    p.write_text(src.decode())
    ctx = ParseContext(path="c.controller.ts", abs_path=p, source=src,
                       repo_root=tmp_path, capture_statements=True)
    routes = {s.handler: s for s in LoopBackParser().parse_file(ctx).statements
              if s.semanticType == "route"}
    assert routes["clean"].requestDTO == "CleanDataDTO"
    assert routes["clean"].responseDTO is None            # Promise<object> → no DTO
    assert routes["find"].requestDTO is None
    assert routes["find"].responseDTO == "ResponseApi"     # first PascalCase non-wrapper
    assert routes["inline"].requestDTO is None             # anonymous inline object → no name


def test_claims_selects_loopback() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(LoopBackParser())
    assert registry.select("x.ts", b"import {get} from '@loopback/rest';").name == "typescript-loopback"
    assert registry.select("x.ts", b"const x = 1;").name == "typescript"  # plain TS -> base
    registry.clear()
