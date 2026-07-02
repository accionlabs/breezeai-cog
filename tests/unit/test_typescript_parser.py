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


def test_type_alias_captured(tmp_path) -> None:
    p = tmp_path / "t.ts"
    p.write_text("type UserId = string;\ntype Point = { x: number };\nconst z = 1;\n")
    ctx = ParseContext(path="t.ts", abs_path=p, source=p.read_bytes(),
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    aliases = [s.name for s in rec.statements if s.nodeType == "type_alias_declaration"]
    assert aliases == ["UserId", "Point"]


def test_class_fields_captured(tmp_path) -> None:
    p = tmp_path / "c.ts"
    p.write_text("class C { count: number = 0; private label = 'x';\n  greet(): number { return this.count; } }\n")
    ctx = ParseContext(path="c.ts", abs_path=p, source=p.read_bytes(),
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    fields = [s.name for s in rec.statements if s.nodeType in ("public_field_definition", "field_definition")]
    assert fields == ["count", "label"]


def test_module_extensions_matched() -> None:
    # .mts/.cts (TS) and .mjs/.cjs (JS) module files must be claimed by the parser.
    parser = TypeScriptParser()
    for ext in (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"):
        assert parser.matches("mod" + ext), ext


def test_inline_callback_body_captured(tmp_path) -> None:
    # Regression (#1): statements & calls inside an anonymous callback must be
    # attributed to the nearest named enclosing function, not dropped.
    p = tmp_path / "cb.ts"
    p.write_text(
        "function processOrder(id) {\n"
        "  orderRepo.findOne(id).then(order => {\n"
        "    order.status = 'PAID';\n"
        "    auditRepo.save(order);\n"
        "    mailer.send(order.email);\n"
        "  });\n"
        "}\n"
    )
    ctx = ParseContext(path="cb.ts", abs_path=p, source=p.read_bytes(),
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    fn = next(f for f in rec.functions if f.name == "processOrder")
    # calls inside the callback now land on the enclosing function
    assert {"save", "send"} <= {c.name for c in fn.calls}
    # the db write inside the callback is detected and parented to processOrder
    db = [s for s in rec.statements if s.semanticType == "db_method_call" and s.parentId == fn.id]
    assert any("auditRepo.save" in s.text for s in db)


def test_top_level_arrow_not_double_emitted(tmp_path) -> None:
    # The wider walk must not re-emit a top-level `const x = () => {}` body (it is
    # already extracted as its own Function).
    p = tmp_path / "d.ts"
    p.write_text("const topFn = (x) => { return doTop(x); };\n")
    ctx = ParseContext(path="d.ts", abs_path=p, source=p.read_bytes(),
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    returns = [s for s in rec.statements if s.nodeType == "return_statement" and "doTop" in s.text]
    assert len(returns) == 1
