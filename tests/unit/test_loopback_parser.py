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


def test_claims_selects_loopback() -> None:
    registry.clear()
    from breezeai_cog.parsers.typescript.parser import TypeScriptParser

    registry.register(TypeScriptParser())
    registry.register(LoopBackParser())
    assert registry.select("x.ts", b"import {get} from '@loopback/rest';").name == "typescript-loopback"
    assert registry.select("x.ts", b"const x = 1;").name == "typescript"  # plain TS -> base
    registry.clear()
