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


def _parse_with_index(parser, files: dict, target: str, tmp_path) -> FileRecord:
    """Write ``files`` to a temp repo, build the C# repo index over them, then parse
    ``target`` with that index — exercises cross-file base-controller resolution."""
    from breezeai_cog.parsers.csharp.imports import build_csharp_index
    for name, src in files.items():
        (tmp_path / name).write_bytes(src)
    index = build_csharp_index(tmp_path, [tmp_path / n for n in files])
    ctx = ParseContext(path=target, abs_path=str(tmp_path / target), source=files[target],
                       repo_root=str(tmp_path), capture_statements=True, resolution_index=index)
    return parser.parse_file(ctx)


# verb on a bare [HttpGet], template on a SEPARATE [Route] — a standard split idiom
SPLIT = b'''
using Microsoft.AspNetCore.Mvc;
namespace Acme {
  [ApiController]
  [Route("api/orders")]
  public class SplitController : ControllerBase {
    [HttpGet]
    [Route("{id}/detail")]
    public IActionResult GetSplit(long id) { return null; }
  }
}
'''

# controller [Route]/[Authorize] live on an abstract base in another file
BASE = b'''
using Microsoft.AspNetCore.Mvc;
namespace Acme {
  [ApiController]
  [Route("api/[controller]")]
  [Authorize]
  public abstract class BaseApiController : ControllerBase { }
}
'''
DERIVED = b'''
using Microsoft.AspNetCore.Mvc;
namespace Acme {
  public class ProductsController : BaseApiController {
    [HttpGet("{id}")]
    public IActionResult Get(long id) { return null; }
  }
}
'''

# base is out-of-repo (not in the index) and the derived controller has no [Route]
EXTERNAL_BASE = b'''
using Microsoft.AspNetCore.Mvc;
namespace Acme {
  public class WidgetsController : Acme.Platform.PlatformControllerBase {
    [HttpGet("{id}")]
    public IActionResult Get(long id) { return null; }
  }
}
'''


def test_split_route_and_verb_compose() -> None:
    # [HttpGet] + separate [Route("{id}/detail")] must compose the full method template
    rec = _parse(AspNetCoreParser(), SPLIT, "Split.cs")
    routes = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert routes == {("GET", "/api/orders/{id}/detail")}


def test_base_controller_route_and_auth_inherited(tmp_path) -> None:
    rec = _parse_with_index(AspNetCoreParser(), {"Base.cs": BASE, "Products.cs": DERIVED},
                            "Products.cs", tmp_path)
    routes = {s.handler: s for s in rec.statements if s.semanticType == "route"}
    assert routes["Get"].endpoint == "/api/Products/{id}"  # base [Route] + [controller]→derived
    assert routes["Get"].authRequired is True              # [Authorize] inherited from base
    assert "Authorize" in routes["Get"].guards


def test_unresolved_base_emits_no_route(tmp_path) -> None:
    # base out-of-repo + no own [Route] → prefix unknowable → honest-null (skip, not "/{id}")
    rec = _parse_with_index(AspNetCoreParser(), {"Widgets.cs": EXTERNAL_BASE},
                            "Widgets.cs", tmp_path)
    assert [s for s in rec.statements if s.semanticType == "route"] == []


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


def test_full_form_attribute_names() -> None:
    # attributes written in full form ([HttpGetAttribute] etc.) must resolve like the short form
    src = (b"using Microsoft.AspNetCore.Mvc;\nnamespace A {\n"
           b"[ApiControllerAttribute] [RouteAttribute(\"api/x\")]\n"
           b"public class XController : ControllerBase {\n"
           b"[HttpGetAttribute(\"{id}\")] public object Get(long id) { return null; }\n} }")
    rec = _parse(AspNetCoreParser(), src, "X.cs")
    routes = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/api/x/{id}") in routes


def test_mvc_convention_route() -> None:
    # classic MVC 5: no [Route] on class/action → endpoint from convention /{controller}/{action}
    src = (b"using Microsoft.AspNetCore.Mvc;\nnamespace A {\n"
           b"public class CatalogController : Controller {\n"
           b"[HttpPost] public object Create(CatalogItem i) { return null; }\n"
           b"[HttpGet] public object Index() { return null; }\n} }")
    rec = _parse(AspNetCoreParser(), src, "CatalogController.cs")
    routes = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert ("POST", "/Catalog/Create") in routes
    assert ("GET", "/Catalog/Index") in routes


def test_mvc_route_registration() -> None:
    # Phase 2: RouteConfig.MapRoute — custom named route + the default template
    src = (b"using System.Web.Mvc;\nnamespace A {\npublic class RouteConfig {\n"
           b"public static void RegisterRoutes(RouteCollection routes) {\n"
           b'routes.MapRoute("ProductDetails", "products/{id}", new { controller = "Catalog", action = "Details" });\n'
           b'routes.MapRoute("Default", "{controller}/{action}/{id}", new { controller = "Home", action = "Index" });\n'
           b"} } }")
    rec = _parse(AspNetCoreParser(), src, "App_Start/RouteConfig.cs")
    routes = {s.endpoint: s for s in rec.statements if s.semanticType == "route"}
    assert routes["/products/{id}"].method == "ANY"
    assert routes["/products/{id}"].handler == "Catalog.Details"
    assert "/{controller}/{action}/{id}" in routes            # default template captured
    assert routes["/{controller}/{action}/{id}"].handler == "Home.Index"


def test_csharp_minimal_apis() -> None:
    rec = _parse(AspNetCoreParser(), CS, "Orders.cs")
    minimal = {(s.method, s.endpoint) for s in rec.statements
               if s.semanticType == "route" and s.handler is None}
    assert ("GET", "/hello") in minimal
    assert ("POST", "/items") in minimal


# The Startup.cs registration block that the benchmark found uncaptured (grounded shape):
# health check, GraphQL HTTP mount via an identifier field, and default-path dev UIs.
STARTUP = b'''
using Microsoft.AspNetCore.Builder;
namespace Acme {
  public class Startup {
    private readonly string _graphQlEndpoint = "/graphql";
    public void Configure(IApplicationBuilder app) {
      app.UseEndpoints(endpoints => {
        endpoints.MapHealthChecks("/health", new HealthCheckOptions());
        endpoints.MapControllerRoute("default", "{controller=Home}/{action=Index}/{id?}");
      });
      app.UseGraphQL<ISchema>(_graphQlEndpoint);
      app.UseGraphQLPlayground();
      app.UseGraphQLVoyager();
    }
  }
}
'''


def test_csharp_endpoint_registrations() -> None:
    rec = _parse(AspNetCoreParser(), STARTUP, "Startup.cs")
    routes = {(s.method, s.endpoint): s for s in rec.statements if s.semanticType == "route"}
    assert ("GET", "/health") in routes and routes[("GET", "/health")].framework == "aspnet"
    # GraphQL HTTP mount: verb POST, path resolved from the _graphQlEndpoint field
    assert ("POST", "/graphql") in routes and routes[("POST", "/graphql")].framework == "graphql"
    # dev UIs at their library-default paths
    assert ("GET", "/ui/playground") in routes
    assert ("GET", "/ui/voyager") in routes
    # MapControllerRoute is a conventional-routing pattern, not a concrete endpoint → skipped
    assert not any("{controller" in (e or "") for _, e in routes)


def test_graphql_mount_defaults_when_no_arg() -> None:
    src = b'''using Microsoft.AspNetCore.Builder;
namespace A { class S { void C(IApplicationBuilder app){ app.UseGraphQL<ISchema>(); } } }'''
    rec = _parse(AspNetCoreParser(), src, "S.cs")
    routes = {(s.method, s.endpoint) for s in rec.statements if s.semanticType == "route"}
    assert ("POST", "/graphql") in routes  # graphql-dotnet convention default


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
