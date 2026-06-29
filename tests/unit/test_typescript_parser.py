"""TypeScript parser extraction tests + schema validation."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser
from breezeai_cog.schemas import FileRecord

SRC = b'''import { Foo } from './foo';
import axios from 'axios';
export { Bar };

@Controller('orders')
export class OrderController extends Base implements IFoo, IBar {
  private count = 0;
  constructor(private repo: OrderRepo) {}

  @Get(':id')
  async getOrder(id: number): Promise<Order> {
    return this.repo.findById(id);
  }
}

export function top(a: number, b = 'x'): string {
  if (a > 0) { return helper(a); }
  return b;
}

const arrow = (x: number): number => x + 1;
'''


def _parse(tmp_path, *, capture=False) -> FileRecord:
    (tmp_path / "foo.ts").write_text("export const Foo = 1;\n")  # makes './foo' resolvable
    p = tmp_path / "order.controller.ts"
    p.write_text(SRC.decode())
    ctx = ParseContext(path="order.controller.ts", abs_path=p, source=SRC, repo_root=tmp_path,
                       capture_statements=capture, text_truncation_limit=1000)
    return TypeScriptParser().parse_file(ctx)


def test_file_level(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert rec.language == "typescript"
    assert "axios" in rec.externalImports
    assert any(p.endswith("foo.ts") for p in rec.importFiles)  # relative import resolved
    assert "OrderController" in rec.exports and "top" in rec.exports and "Bar" in rec.exports


def test_class(tmp_path) -> None:
    rec = _parse(tmp_path)
    cls = next(c for c in rec.classes if c.name == "OrderController")
    assert cls.type == "class" and cls.extends == "Base"
    assert cls.implements == ["IFoo", "IBar"]
    assert [d.name for d in cls.decorators] == ["Controller"]
    assert cls.constructorParams == [
        __import__("breezeai_cog.schemas", fromlist=["ConstructorParam"]).ConstructorParam(name="repo", type="OrderRepo")
    ]


def test_methods_and_functions(tmp_path) -> None:
    rec = _parse(tmp_path)
    methods = {f.name: f for f in rec.functions if f.type in ("method", "constructor")}
    get = methods["getOrder"]
    assert get.returnType == "Promise<Order>"
    assert [d.name for d in get.decorators] == ["Get"]
    assert get.params[0].name == "id" and get.params[0].type == "number"
    assert "findById" in [c.name for c in get.calls]
    assert get.parentId == next(c for c in rec.classes).id  # HAS_METHOD wiring

    top = next(f for f in rec.functions if f.name == "top")
    assert top.type == "function" and top.returnType == "string"
    arrow = next(f for f in rec.functions if f.name == "arrow")
    assert arrow.type == "arrow_function" and arrow.returnType == "number"


def test_statements_flat_and_gated(tmp_path) -> None:
    assert _parse(tmp_path, capture=False).statements == []
    rec = _parse(tmp_path, capture=True)
    top = next(f for f in rec.functions if f.name == "top")
    node_types = {s.nodeType for s in rec.statements if s.parentId == top.id}
    assert "if_statement" in node_types and "return_statement" in node_types


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, capture=True)
    schema = FileRecord.model_json_schema(by_alias=True)
    errors = list(Draft202012Validator(schema).iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
