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
    assert rec.importFiles == []  # no repo index here -> nothing resolves to a file


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


PROPERTY_SRC = b'''
public class Model
{
    public int Count { get; set; }
    public string Name => _n;
    private string _n;
    public void M() { var x = 1; }
}
'''


def test_property_declaration_captured(tmp_path) -> None:
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(PROPERTY_SRC.decode())
    ctx = ParseContext(path=REL, abs_path=p, source=PROPERTY_SRC, repo_root=tmp_path,
                       capture_statements=True)
    rec = CSharpParser().parse_file(ctx)
    props = [s.name for s in rec.statements if s.nodeType == "property_declaration"]
    assert props == ["Count", "Name"]


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, capture=True)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_chain_multi_hit(tmp_path) -> None:
    # #4: chained db calls each classified — base carries the first hit, a synthetic
    # same-span record carries the second.
    src = "class C { void M(Repo repo){ var r = repo.CreateQueryBuilder().Where(x).ToListAsync(); } }"
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)
    ctx = ParseContext(path=REL, abs_path=p, source=src.encode(), repo_root=tmp_path,
                       capture_statements=True)
    rec = CSharpParser().parse_file(ctx)
    methods = {s.method for s in rec.statements if s.semanticType == "db_method_call"}
    assert {"CreateQueryBuilder", "ToListAsync"} <= methods


def test_endpoint_interpolated_string(tmp_path) -> None:
    # #3: C# interpolated string $"/users/{id}" -> /users/{id}.
    src = 'class C { void M(int id){ httpClient.GetAsync($"/users/{id}"); } }'
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)
    ctx = ParseContext(path=REL, abs_path=p, source=src.encode(), repo_root=tmp_path,
                       capture_statements=True)
    rec = CSharpParser().parse_file(ctx)
    assert any(s.semanticType == "api_call" and s.endpoint == "/users/{id}" for s in rec.statements)


# --- cross-file resolution (namespace/type -> file) -----------------------------------

def _parse_repo(tmp_path, files: dict[str, str], target: str) -> FileRecord:
    """Write a multi-file C# repo, run ``build_index``, parse ``target`` with the index."""
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    parser = CSharpParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.cs")))
    p = tmp_path / target
    ctx = ParseContext(path=target, abs_path=p, source=p.read_bytes(),
                       repo_root=tmp_path, resolution_index=index)
    return parser.parse_file(ctx)


def test_referenced_type_resolves_to_file(tmp_path) -> None:
    # A `using`d namespace's type referenced as a field resolves to the declaring file,
    # and a call on that field becomes a cross-file CALLS edge.
    rec = _parse_repo(tmp_path, {
        "Repo/OrderRepo.cs":
            "namespace Acme.Repo;\npublic class OrderRepo { public object FindById(int id){return null;} }\n",
        "Controllers/OrderController.cs":
            "using Acme.Repo;\nnamespace Acme.Controllers;\n"
            "public class OrderController {\n"
            "    private readonly OrderRepo repo;\n"
            "    public OrderController(OrderRepo repo){ this.repo = repo; }\n"
            "    public object Get(int id){ return repo.FindById(id); }\n"
            "}\n",
    }, "Controllers/OrderController.cs")
    assert "Repo/OrderRepo.cs" in rec.importFiles          # IMPORTS edge
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("FindById") == "Repo/OrderRepo.cs"    # cross-file CALLS edge


def test_global_using_alias_and_static_resolve(tmp_path) -> None:
    rec = _parse_repo(tmp_path, {
        "GlobalUsings.cs": "global using Acme.Data;\n",
        "Data/OrderRepo.cs":
            "namespace Acme.Data;\npublic class OrderRepo { public object FindById(int id){return null;} }\n",
        "Helpers/MathUtil.cs":
            "namespace Acme.Helpers;\npublic static class MathUtil { public static int Add(int a,int b){return a+b;} }\n",
        "App/Consumer.cs":
            "using static Acme.Helpers.MathUtil;\n"
            "using Repo = Acme.Data.OrderRepo;\n"
            "namespace Acme.App;\n"
            "public class Consumer {\n"
            "    private readonly OrderRepo direct;   // via global using\n"
            "    private readonly Repo aliased;       // via alias\n"
            "    public void Run(int id){ direct.FindById(id); aliased.FindById(id); MathUtil.Add(1,2); }\n"
            "}\n",
    }, "App/Consumer.cs")
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("FindById") == "Data/OrderRepo.cs"    # global-using + alias field types
    assert calls.get("Add") == "Helpers/MathUtil.cs"       # `using static` type binding
    assert {"Data/OrderRepo.cs", "Helpers/MathUtil.cs"} <= set(rec.importFiles)


def test_ambiguous_type_does_not_bind(tmp_path) -> None:
    # Precision-first: a type declared in >1 in-repo file must NOT resolve (else a shared
    # name would create false hub edges and collapse unrelated files into one cluster).
    rec = _parse_repo(tmp_path, {
        "A/Dup.cs": "namespace Shared;\npublic class Dup { public void Ping(){} }\n",
        "B/Dup.cs": "namespace Shared;\npublic class Dup { public void Ping(){} }\n",
        "App/User.cs":
            "using Shared;\nnamespace App;\n"
            "public class User { private readonly Dup d; public void Go(){ d.Ping(); } }\n",
    }, "App/User.cs")
    assert rec.importFiles == []                           # ambiguous -> no internal import
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("Ping") is None                       # ambiguous -> unresolved call


def test_same_project_type_wins(tmp_path) -> None:
    # Identical FQCN in two projects. C# resolves the consumer's OWN-project copy
    # (CS0436 "source wins"); a tie purely between two OTHER projects is CS0433 -> refuse.
    files = {
        "ProjA/ProjA.csproj": "<Project></Project>",
        "ProjA/IProvider.cs": "namespace Shared;\npublic interface IProvider { void Do(); }\n",
        "ProjB/ProjB.csproj": "<Project></Project>",
        "ProjB/IProvider.cs": "namespace Shared;\npublic interface IProvider { void Do(); }\n",
        "ProjB/Widget.cs":
            "using Shared;\nnamespace App;\n"
            "public class Widget { private readonly IProvider p; public void Run(){ p.Do(); } }\n",
        "ProjC/ProjC.csproj": "<Project></Project>",
        "ProjC/Thing.cs":
            "using Shared;\nnamespace App2;\n"
            "public class Thing { private readonly IProvider q; public void Go(){ q.Do(); } }\n",
    }
    # consumer inside ProjB -> binds ProjB's copy
    rec = _parse_repo(tmp_path, files, "ProjB/Widget.cs")
    assert rec.importFiles == ["ProjB/IProvider.cs"]
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("Do") == "ProjB/IProvider.cs"
    # consumer in ProjC -> tie between ProjA/ProjB, none local -> refuse (no guess)
    rec2 = _parse_repo(tmp_path, files, "ProjC/Thing.cs")
    assert rec2.importFiles == []
    calls2 = {c.name: c.path for f in rec2.functions for c in f.calls}
    assert calls2.get("Do") is None


# --- inherited base-class & extension-method call resolution --------------------------

def test_inherited_base_method_resolves(tmp_path) -> None:
    # `this.M()` where M is declared on a base class in ANOTHER file → the base's file.
    rec = _parse_repo(tmp_path, {
        "Svc/BaseService.cs":
            "namespace App;\npublic class BaseService { protected void LogAudit(string m){} }\n",
        "Svc/OrderService.cs":
            "namespace App;\n"
            "public class OrderService : BaseService {\n"
            "    public void Place(){ this.LogAudit(\"x\"); }\n"
            "}\n",
    }, "Svc/OrderService.cs")
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("LogAudit") == "Svc/BaseService.cs"


def test_extension_method_resolves(tmp_path) -> None:
    # `recv.M()` where M is `static M(this T…)` and recv's type is the external T → the
    # extension's defining file (Phase 2 finds no in-repo member, extension tier resolves).
    rec = _parse_repo(tmp_path, {
        "Data/DataLayer.cs":
            "using System.Data;\nnamespace App.Data;\n"
            "public static class DataLayer {\n"
            "    public static object GetSqlData(this IDataReader r, string sql){ return null; }\n"
            "}\n",
        "Repos/ReservationRepository.cs":
            "using System.Data;\nusing App.Data;\nnamespace App.Repos;\n"
            "public class ReservationRepository {\n"
            "    public void Load(IDataReader reader){ var d = reader.GetSqlData(\"q\"); }\n"
            "}\n",
    }, "Repos/ReservationRepository.cs")
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("GetSqlData") == "Data/DataLayer.cs"


def test_external_method_call_stays_null(tmp_path) -> None:
    # Precision: a call on an external (BCL) receiver with no in-repo extension stays null —
    # the new tiers must never fabricate an edge.
    rec = _parse_repo(tmp_path, {
        "Repos/Repo.cs":
            "using System.Data;\nnamespace App.Repos;\n"
            "public class Repo { public void Load(IDataReader reader){ reader.Read(); } }\n",
    }, "Repos/Repo.cs")
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("Read") is None


def test_ambiguous_extension_stays_null(tmp_path) -> None:
    # The same extension (method, this-type) defined in >1 file → ambiguous → None.
    rec = _parse_repo(tmp_path, {
        "Ext/A.cs":
            "using System.Data;\nnamespace App.A;\n"
            "public static class A { public static int M(this IDataReader r){ return 1; } }\n",
        "Ext/B.cs":
            "using System.Data;\nnamespace App.B;\n"
            "public static class B { public static int M(this IDataReader r){ return 2; } }\n",
        "Use/User.cs":
            "using System.Data;\nusing App.A;\nusing App.B;\nnamespace App.Use;\n"
            "public class User { public void Go(IDataReader reader){ reader.M(); } }\n",
    }, "Use/User.cs")
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("M") is None


def test_partial_class_method_resolves_to_declaring_file(tmp_path) -> None:
    # A base split across partial files: `this.M()` resolves to the partial file that
    # actually declares M (not merely the first-seen partial).
    rec = _parse_repo(tmp_path, {
        "Base.Part1.cs": "namespace App;\npublic partial class Base { }\n",
        "Base.Part2.cs": "namespace App;\npublic partial class Base { public void Helper(){} }\n",
        "Derived.cs":
            "namespace App;\npublic class Derived : Base { public void F(){ this.Helper(); } }\n",
    }, "Derived.cs")
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("Helper") == "Base.Part2.cs"


def test_partial_class_ambiguous_method_stays_null(tmp_path) -> None:
    # A method name declared in BOTH partial files can't be pinned to one file → None.
    rec = _parse_repo(tmp_path, {
        "Base.Part1.cs": "namespace App;\npublic partial class Base { public void Dup(){} }\n",
        "Base.Part2.cs": "namespace App;\npublic partial class Base { public void Dup(int x){} }\n",
        "Derived.cs":
            "namespace App;\npublic class Derived : Base { public void F(){ this.Dup(); } }\n",
    }, "Derived.cs")
    calls = {c.name: c.path for f in rec.functions for c in f.calls}
    assert calls.get("Dup") is None
