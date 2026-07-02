"""ASP.NET framework parsers (C# + VB): controller routes (off the record), minimal-API
routes (AST walk), route attributes (spec C5), capture-gating, and parser selection."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.csharp_aspnet.parser import AspNetCoreParser
from breezeai_cog.parsers.vb.parser import VbParser
from breezeai_cog.parsers.vb_aspnet.parser import VbAspNetParser
from breezeai_cog.schemas import FileRecord

CS = b'''
using Microsoft.AspNetCore.Mvc;
namespace Acme {
  [ApiController]
  [Route("api/[controller]")]
  [Authorize]
  public class OrdersController : ControllerBase {
    [HttpGet("{id}")]
    public async Task<Order> Get(long id) { return null; }

    [HttpPost]
    public ActionResult<Order> Create([FromBody] OrderDto dto) { return null; }
  }

  public static class Program {
    public static void Main() {
      var app = builder.Build();
      app.MapGet("/hello", () => "hi");
      app.MapPost("/items", Handler);
    }
  }
}
'''

VB = b'''Imports Microsoft.AspNetCore.Mvc
Namespace Acme
  <ApiController>
  <Route("api/orders")>
  Public Class OrdersController
    Inherits ControllerBase
    <HttpGet("{id}")>
    Public Function GetItem(id As Long) As Task(Of Order)
      Return Nothing
    End Function
    <HttpPost>
    Public Function Create(<FromBody> dto As OrderDto) As ActionResult(Of Order)
      Return Nothing
    End Function
  End Class
End Namespace'''


def _parse(parser, src, name, *, capture=True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None, capture_statements=capture)
    return parser.parse_file(ctx)


def test_routes_require_capture(tmp_path) -> None:
    rec = _parse(AspNetCoreParser(), CS, "Orders.cs", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_csharp_controller_routes() -> None:
    rec = _parse(AspNetCoreParser(), CS, "Orders.cs")
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/api/Orders/{id}") in routes  # [controller] token expanded
    assert ("POST", "/api/Orders") in routes
    assert rec.framework == "aspnet"
    fn_ids = {f.id for f in rec.functions}
    assert routes[("GET", "/api/Orders/{id}")].parentId in fn_ids  # parented to handler


def test_csharp_route_attributes() -> None:
    rec = _parse(AspNetCoreParser(), CS, "Orders.cs")
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route" and s.handler}
    assert routes["Create"].requestDTO == "OrderDto"       # [FromBody] type
    assert routes["Create"].responseDTO == "Order"          # ActionResult<Order> unwrapped
    assert routes["Get"].responseDTO == "Order"             # Task<Order> unwrapped
    assert routes["Create"].isRegex is False
    assert routes["Get"].authRequired is True and "Authorize" in routes["Get"].guards


def test_csharp_minimal_apis() -> None:
    rec = _parse(AspNetCoreParser(), CS, "Orders.cs")
    minimal = {(s.method, s.endpoint) for s in rec.statements
               if s.semanticType == "route" and s.handler is None}
    assert ("GET", "/hello") in minimal
    assert ("POST", "/items") in minimal


def test_vb_controller_routes() -> None:
    rec = _parse(VbAspNetParser(), VB, "Orders.vb")
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/api/orders/{id}") in routes
    assert ("POST", "/api/orders") in routes
    by_handler = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    assert by_handler["Create"].requestDTO == "OrderDto"
    assert by_handler["Create"].responseDTO == "Order"
    assert rec.framework == "aspnet"


def test_non_controller_has_no_routes() -> None:
    src = b"using System;\nnamespace X { public class Plain { public int Add(int a){ return a; } } }"
    rec = _parse(AspNetCoreParser(), src, "Plain.cs")
    assert [s for s in rec.statements if s.semanticType == "route"] == []


def test_output_validates() -> None:
    for parser, src, name in [(AspNetCoreParser(), CS, "Orders.cs"), (VbAspNetParser(), VB, "Orders.vb")]:
        rec = _parse(parser, src, name)
        errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                      .iter_errors(json.loads(to_line(rec))))
        assert not errors, errors


def test_selection() -> None:
    registry.clear()
    for p in (CSharpParser(), AspNetCoreParser(), VbParser(), VbAspNetParser()):
        registry.register(p)
    assert registry.select("Orders.cs", CS).name == "csharp-aspnet"
    assert registry.select("plain.cs", b"class C {}").name == "csharp"
    assert registry.select("Orders.vb", VB).name == "vb-aspnet"
    assert registry.select("plain.vb", b"Class C\nEnd Class").name == "vb"
    registry.clear()
