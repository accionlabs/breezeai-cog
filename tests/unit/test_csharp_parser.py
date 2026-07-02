"""C# parser extraction tests + statement/detection wiring + schema validation."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.schemas import ConstructorParam, FileRecord

SRC = b'''
using System;
using Microsoft.AspNetCore.Mvc;

namespace Acme.Orders
{
    [ApiController]
    [Route("api/orders")]
    public class OrderController : ControllerBase, IThing
    {
        private readonly IOrderRepo _repo;

        public OrderController(IOrderRepo repo) { _repo = repo; }

        [HttpGet("{id}")]
        public async Task<Order> GetOrder([FromRoute] long id)
        {
            var order = await _repo.FindAsync(id);
            return order;
        }
    }

    public interface IThing { }
    public enum Status { Open, Closed }
    public struct Point { public int X; }
    public record Money(decimal Amount);
}
'''

REL = "src/Order.cs"


def _parse(tmp_path, *, capture=False) -> FileRecord:
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SRC.decode())
    ctx = ParseContext(path=REL, abs_path=p, source=SRC, repo_root=tmp_path, capture_statements=capture)
    return CSharpParser().parse_file(ctx)


def test_language_and_imports(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert rec.language == "csharp"
    assert "System" in rec.externalImports
    assert "Microsoft.AspNetCore.Mvc" in rec.externalImports
    assert rec.importFiles == []  # C# usings don't resolve to files


def test_types(tmp_path) -> None:
    rec = _parse(tmp_path)
    by_name = {c.name: c for c in rec.classes}
    assert by_name["OrderController"].type == "class"
    assert by_name["IThing"].type == "interface"
    assert by_name["Status"].type == "enum"
    assert by_name["Point"].type == "struct"
    assert by_name["Money"].type == "record"
    ctrl = by_name["OrderController"]
    assert ctrl.extends == "ControllerBase" and ctrl.implements == ["IThing"]
    assert {d.name for d in ctrl.decorators} == {"ApiController", "Route"}
    assert ctrl.constructorParams == [ConstructorParam(name="repo", type="IOrderRepo")]
    assert ctrl.visibility == "public"
    assert by_name["Money"].constructorParams == [ConstructorParam(name="Amount", type="decimal")]


def test_methods(tmp_path) -> None:
    rec = _parse(tmp_path)
    get = next(f for f in rec.functions if f.name == "GetOrder")
    assert get.type == "method" and get.visibility == "public" and get.returnType == "Task<Order>"
    assert [d.name for d in get.decorators] == ["HttpGet"]
    assert get.params[0].name == "id" and get.params[0].type == "long"
    assert [d.name for d in get.params[0].decorators] == ["FromRoute"]
    assert "FindAsync" in [c.name for c in get.calls]
    ctrl = next(c for c in rec.classes if c.name == "OrderController")
    assert get.parentId == ctrl.id  # HAS_METHOD wiring
    assert any(f.type == "constructor" for f in rec.functions)


def test_statements_and_detection(tmp_path) -> None:
    assert _parse(tmp_path, capture=False).statements == []
    rec = _parse(tmp_path, capture=True)
    db = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert db and db[0].dataAccessHint == "entity_framework"  # _repo.FindAsync -> EF
    # statements are flat + parented to their owning function/class
    fn_ids = {f.id for f in rec.functions}
    cls_ids = {c.id for c in rec.classes}
    assert all(s.parentId in fn_ids | cls_ids for s in rec.statements)


GENERIC_SRC = b'''
namespace Acme
{
    public class Repo
    {
        public Order Load(long id)
        {
            var a = _ctx.GetById<Order>(id);
            var b = Build<Widget>();
            return a;
        }
    }
}
'''


def test_generic_method_call_strips_type_args(tmp_path) -> None:
    # Regression: `Foo<T>()` must resolve to the bare name `Foo` (not `Foo<T>`) on
    # both CALLS edges and captured statements, else db/api classification and
    # call-path matching miss every generic invocation.
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(GENERIC_SRC.decode())
    ctx = ParseContext(path=REL, abs_path=p, source=GENERIC_SRC, repo_root=tmp_path,
                       capture_statements=True)
    rec = CSharpParser().parse_file(ctx)

    load = next(f for f in rec.functions if f.name == "Load")
    call_names = [c.name for c in load.calls]
    assert "GetById" in call_names and "Build" in call_names
    assert not any("<" in n for n in call_names)


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, capture=True)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
