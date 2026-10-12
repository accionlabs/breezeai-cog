"""Role-table tests for ASP.NET (docs/entry-point-roles/aspnet.md)."""

from __future__ import annotations

import pytest

EP = {"route", "eventbus_consumer", "timer"}

USINGS = "using Microsoft.AspNetCore.Mvc;\nusing Microsoft.AspNetCore.Authorization;\n"

CONTROLLER = (
    USINGS
    + """
namespace Shop;

[ApiController]
[Route("api/orders")]
[Authorize]
public class OrdersController : ControllerBase
{
    [HttpGet("{id}")]
    public IActionResult Get(int id) { return Ok(); }
}
"""
)

MVC = """using System.Web.Mvc;

namespace Shop;

public class HomeController : Controller
{
    public ActionResult About() { return View(); }
}
"""

ROUTE_CONFIG = """using System.Web.Mvc;
using System.Web.Routing;

public class RouteConfig
{
    public static void RegisterRoutes(RouteCollection routes)
    {
        routes.MapRoute("Default", "{controller}/{action}/{id}",
            new { controller = "Home", action = "Index" });
    }
}
"""

PROGRAM = """using Microsoft.AspNetCore.Builder;

var builder = WebApplication.CreateBuilder(args);
var app = builder.Build();
app.UseAuthentication();
app.UseMiddleware<AuditMiddleware>();
app.MapControllers();
app.MapGet("/ping", () => "pong");
app.MapGet("/orders", GetOrders);
var api = app.MapGroup("/api");
api.MapGet("/items", () => "items");
app.MapHealthChecks("/health");
app.MapGraphQL();
app.UseGraphQLPlayground();
app.Run();

static string GetOrders() => "orders";
"""


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def _at(stmts, path, line):
    return [s for s in _eps(stmts) if s.path == path and s.startLine == line]


def _program_line(snippet: str) -> int:
    return next(i for i, line in enumerate(PROGRAM.splitlines(), 1) if snippet in line)


@pytest.fixture
def program(analyze_files):
    return analyze_files({"Program.cs": PROGRAM})


def test_http_verb_attribute_is_binding(analyze_files) -> None:
    stmts = analyze_files({"OrdersController.cs": CONTROLLER})
    [r] = _eps(stmts)
    assert (r.semanticType, r.method, r.routeKind, r.endpoint) == (
        "route",
        "GET",
        "route",
        "/api/orders/{id}",
    )
    assert r.framework == "aspnet"
    lines = CONTROLLER.splitlines()
    attr = lines.index('    [HttpGet("{id}")]') + 1  # the declaration starts at its attributes
    assert r.handler == "Get" and attr <= r.handlerLine <= attr + 1


def test_controller_route_attribute_is_wiring(analyze_files) -> None:
    stmts = analyze_files({"OrdersController.cs": CONTROLLER})
    cls_line = CONTROLLER.splitlines().index("public class OrdersController : ControllerBase") + 1
    assert _at(stmts, "OrdersController.cs", cls_line) == []
    assert all(r.endpoint.startswith("/api/orders") for r in _eps(stmts))


def test_conventional_mvc_action_is_binding(analyze_files) -> None:
    stmts = analyze_files({"HomeController.cs": MVC})
    [r] = _eps(stmts)
    assert (r.method, r.endpoint, r.handler) == ("GET", "/Home/About", "About")


@pytest.mark.xfail(strict=True, reason="convention route registration recorded as a route")
def test_maproute_registration_is_wiring(analyze_files) -> None:
    stmts = analyze_files({"RouteConfig.cs": ROUTE_CONFIG})
    assert _eps(stmts) == []


def test_authorize_attribute_is_guard(analyze_files) -> None:
    stmts = analyze_files({"OrdersController.cs": CONTROLLER})
    [r] = _eps(stmts)
    assert "Authorize" in (r.guards or [])


def test_middleware_is_guard_without_record(program) -> None:
    for snippet in ("app.UseAuthentication()", "app.UseMiddleware<AuditMiddleware>()"):
        assert _at(program, "Program.cs", _program_line(snippet)) == []


def test_map_controllers_is_wiring(program) -> None:
    assert _at(program, "Program.cs", _program_line("app.MapControllers()")) == []


def test_minimal_api_inline_handler_is_binding(program) -> None:
    line = _program_line('app.MapGet("/ping"')
    [r] = _at(program, "Program.cs", line)
    assert (r.method, r.endpoint, r.routeKind) == ("GET", "/ping", "route")
    assert not r.handler and r.handlerLine == line


def test_minimal_api_named_handler_is_binding(program) -> None:
    [r] = _at(program, "Program.cs", _program_line('app.MapGet("/orders"'))
    assert r.endpoint == "/orders"
    assert (r.handler, r.handlerLine) == ("GetOrders", _program_line("static string GetOrders"))


@pytest.mark.xfail(strict=True, reason="MapGroup prefix not applied to its endpoints")
def test_map_group_is_wiring(program) -> None:
    assert _at(program, "Program.cs", _program_line('app.MapGroup("/api")')) == []
    [r] = _at(program, "Program.cs", _program_line('api.MapGet("/items"'))
    assert r.endpoint == "/api/items"


@pytest.mark.xfail(strict=True, reason="framework health-check endpoint recorded as a route")
def test_health_checks_is_none(program) -> None:
    assert _at(program, "Program.cs", _program_line("app.MapHealthChecks")) == []


@pytest.mark.xfail(strict=True, reason="GraphQL transport mount recorded as a route")
def test_map_graphql_is_wiring(program) -> None:
    assert _at(program, "Program.cs", _program_line("app.MapGraphQL()")) == []


@pytest.mark.xfail(strict=True, reason="GraphQL dev-UI middleware recorded as a route")
def test_graphql_ui_is_none(program) -> None:
    assert _at(program, "Program.cs", _program_line("app.UseGraphQLPlayground()")) == []


def test_startup_is_none(program) -> None:
    for snippet in ("WebApplication.CreateBuilder", "app.Run()"):
        assert _at(program, "Program.cs", _program_line(snippet)) == []


@pytest.mark.xfail(strict=True, reason="C# HttpClient call not recorded as api_call")
def test_http_client_call_is_outbound(analyze_files) -> None:
    src = """using System.Net.Http;

public class OrdersClient
{
    private readonly HttpClient _http = new HttpClient();
    public async Task<string> Load() { return await _http.GetStringAsync("/api/orders"); }
}
"""
    stmts = analyze_files({"OrdersClient.cs": src})
    assert _eps(stmts) == []
    assert any(s.semanticType == "api_call" for s in stmts)


@pytest.mark.xfail(strict=True, reason="Razor page record has no handler")
def test_razor_page_directive_is_binding(analyze_files) -> None:
    page = '@page "/orders"\n@model OrdersModel\n<h1>Orders</h1>\n'
    model = """using Microsoft.AspNetCore.Mvc.RazorPages;

public class OrdersModel : PageModel
{
    public void OnGet() { }
}
"""
    stmts = analyze_files(
        {"Pages/Orders.cshtml": page, "Pages/Orders.cshtml.cs": model}, templates=True
    )
    [r] = [s for s in _eps(stmts) if s.path == "Pages/Orders.cshtml"]
    assert (r.semanticType, r.routeKind, r.endpoint) == ("route", "page", "/orders")
    assert r.handler and r.handlerLine


@pytest.mark.xfail(strict=True, reason="Hangfire recurring job not recorded as a timer")
def test_recurring_job_is_timer(analyze_files) -> None:
    src = """using Hangfire;

public static class Jobs
{
    public static void Register()
    {
        RecurringJob.AddOrUpdate<Cleanup>("cleanup", c => c.Run(), "0 2 * * *");
    }
}
"""
    stmts = analyze_files({"Jobs.cs": src})
    [r] = _eps(stmts)
    assert (r.semanticType, r.endpoint) == ("timer", "0 2 * * *")
