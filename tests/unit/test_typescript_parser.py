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


# G2: arrow functions attached as object-literal properties (resolver maps, service
# objects) are lifted into the function inventory, named by their key-trail.
OBJ_FN_SRC = b'''const DENOM = { PI: 3.14 };                 // pure data: NOT descended

export const api = {                          // depth-1 service object
  getUser: async (id) => { return fetch(id); },
  saveUser: (u) => { return db.put(u); },
};

export const resolvers = {
  DateTime: DateTimeResolver,                 // non-function property: skipped
  Query: {                                    // depth-2 grouping
    thing: async (_, { id }, ctx) => { return ctx.get(id); },
  },
  Mutation: {
    makeThing: (_, { input }, ctx) => { return ctx.create(input); },
  },
};

function plain() { return 1; }
'''


def test_object_property_functions_captured(tmp_path) -> None:
    p = tmp_path / "resolvers.ts"
    p.write_bytes(OBJ_FN_SRC)
    ctx = ParseContext(path="resolvers.ts", abs_path=p, source=OBJ_FN_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    names = {f.name for f in rec.functions}
    # depth-1 service arrows, named by key-trail.
    assert "api.getUser" in names and "api.saveUser" in names
    # depth-2 resolver arrows, full trail through the grouping object.
    assert "resolvers.Query.thing" in names
    assert "resolvers.Mutation.makeThing" in names
    # plain function still captured; non-function property is not.
    assert "plain" in names
    assert "resolvers.DateTime" not in names
    # pure-data object is never descended (function-bearing guard).
    assert not any(n.startswith("DENOM") for n in names)
    # body is real: params + the service-call edge are captured.
    thing = next(f for f in rec.functions if f.name == "resolvers.Query.thing")
    assert len(thing.params) == 3 and any(c.name == "get" for c in thing.calls)


def test_object_function_ids_are_unique(tmp_path) -> None:
    p = tmp_path / "resolvers.ts"
    p.write_bytes(OBJ_FN_SRC)
    ctx = ParseContext(path="resolvers.ts", abs_path=p, source=OBJ_FN_SRC,
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    ids = [f.id for f in rec.functions]
    assert len(ids) == len(set(ids))  # deterministic, disambiguated ids


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


def test_chain_inner_call_classified(tmp_path) -> None:
    # #4: a db method that is NOT the outermost call in a chain must still be detected.
    p = tmp_path / "chain.ts"
    p.write_text("function f(repo){ const rows = repo.createQueryBuilder('o').where('x').getMany(); }")
    ctx = ParseContext(path="chain.ts", abs_path=p, source=p.read_bytes(),
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    db = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert any(s.method == "createQueryBuilder" and s.dataAccessHint == "typeorm" for s in db)


def test_multi_hit_emits_synthetic(tmp_path) -> None:
    # #4: one statement with an api call AND a db call yields both (base + synthetic),
    # each single-valued, at the same span.
    p = tmp_path / "multi.ts"
    p.write_text("function f(){ const d = http.get('/a').then(r => auditRepo.save(r)); }")
    ctx = ParseContext(path="multi.ts", abs_path=p, source=p.read_bytes(),
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    kinds = {s.semanticType for s in rec.statements if s.semanticType}
    assert {"api_call", "db_method_call"} <= kinds


def test_control_statement_not_mislabeled(tmp_path) -> None:
    # #4/smear: a db call nested in an if/for body must not tag the if/for themselves.
    p = tmp_path / "smear.ts"
    p.write_text("function h(o){ if(o.length>0){ for(const x of o){ repo.save(x); } } }")
    ctx = ParseContext(path="smear.ts", abs_path=p, source=p.read_bytes(),
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    control = [s for s in rec.statements if s.nodeType in ("if_statement", "for_in_statement", "for_statement")]
    assert control and all(s.semanticType is None for s in control)
    assert any(s.semanticType == "db_method_call" for s in rec.statements)


def _api(tmp_path, body):
    p = tmp_path / "u.ts"
    p.write_text(body)
    ctx = ParseContext(path="u.ts", abs_path=p, source=p.read_bytes(),
                       repo_root=tmp_path, capture_statements=True)
    rec = TypeScriptParser().parse_file(ctx)
    return [(s.method, s.endpoint) for s in rec.statements if s.semanticType == "api_call"]


def test_endpoint_template_string(tmp_path) -> None:
    # #3: template literal -> path with {param}; leading base/host var dropped.
    assert _api(tmp_path, "function f(baseURL,id){ axios.get(`${baseURL}/users/${id}`); }") \
        == [("GET", "/users/{id}")]
    assert _api(tmp_path, "function f(id){ axios.get(`/api/${id}`); }") == [("GET", "/api/{id}")]


def test_endpoint_concatenation(tmp_path) -> None:
    # #3: string concatenation -> path with {param}.
    assert _api(tmp_path, "function f(id){ axios.get('/a/' + id + '/b'); }") == [("GET", "/a/{id}/b")]


def test_endpoint_config_object(tmp_path) -> None:
    # #3: axios({ url, method }) — both were missed before.
    assert _api(tmp_path, "function f(){ axios({ url: '/orders', method: 'get' }); }") \
        == [("GET", "/orders")]


def test_endpoint_verb_first_arg(tmp_path) -> None:
    # #3: request('GET', url) — verb is the method, URL is the 2nd arg (not the verb).
    assert _api(tmp_path, "function f(){ http.request('GET', '/orders'); }") == [("GET", "/orders")]
    # unresolvable URL (an identifier) yields no endpoint rather than the verb string
    assert _api(tmp_path, "function f(u){ http.request('GET', u); }") == [("GET", None)]
