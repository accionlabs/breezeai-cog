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
    # Decision Q3: handlerLine is the handler Function's start line (attributes included).
    assert r.handler == "Get" and r.handlerLine == attr


def test_controller_route_attribute_is_wiring(analyze_files) -> None:
    stmts = analyze_files({"OrdersController.cs": CONTROLLER})
    cls_line = CONTROLLER.splitlines().index("public class OrdersController : ControllerBase") + 1
    assert _at(stmts, "OrdersController.cs", cls_line) == []
    assert all(r.endpoint.startswith("/api/orders") for r in _eps(stmts))


def test_conventional_mvc_action_is_binding(analyze_files) -> None:
    stmts = analyze_files({"HomeController.cs": MVC})
    [r] = _eps(stmts)
    assert (r.method, r.endpoint, r.handler) == ("GET", "/Home/About", "About")


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


def test_map_group_is_wiring(program) -> None:
    assert _at(program, "Program.cs", _program_line('app.MapGroup("/api")')) == []
    [r] = _at(program, "Program.cs", _program_line('api.MapGet("/items"'))
    assert r.endpoint == "/api/items"


def test_health_checks_is_binding_without_handler(program) -> None:
    # Decision Q2: a framework-served URL is an entry point with an empty handler.
    [r] = _at(program, "Program.cs", _program_line("app.MapHealthChecks"))
    assert (r.semanticType, r.endpoint, r.handler) == ("route", "/health", None)


def test_map_graphql_is_wiring(program) -> None:
    assert _at(program, "Program.cs", _program_line("app.MapGraphQL()")) == []


def test_graphql_ui_is_binding_without_handler(program) -> None:
    # Decision Q2: the dev UI is a real URL served by the framework; no handler of ours.
    [r] = _at(program, "Program.cs", _program_line("app.UseGraphQLPlayground()"))
    assert r.semanticType == "route" and r.handler is None


def test_startup_is_none(program) -> None:
    for snippet in ("WebApplication.CreateBuilder", "app.Run()"):
        assert _at(program, "Program.cs", _program_line(snippet)) == []


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
    # The PageModel class serves the page; it is declared in another file, so no handlerLine
    # here (its file goes to handlerPath).
    assert (r.handler, r.handlerLine) == ("OrdersModel", None)


def test_blazor_page_handler_is_its_component(analyze_files) -> None:
    stmts = analyze_files({"Pages/Counter.razor": '@page "/counter"\n<h1>Counter</h1>\n'},
                          templates=True)
    [r] = [s for s in _eps(stmts) if s.path == "Pages/Counter.razor"]
    assert (r.endpoint, r.handler) == ("/counter", "Counter")


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


def test_repeated_verb_attribute_is_one_binding(analyze_files) -> None:
    src = USINGS + """
[ApiController]
[Route("api/orders")]
public class OrdersController : ControllerBase
{
    [HttpPost]
    [HttpPost]
    [HttpOptions]
    [Route("filter")]
    public IActionResult Filter() { return Ok(); }
}
"""
    stmts = analyze_files({"OrdersController.cs": src})
    got = sorted((r.method, r.endpoint) for r in _eps(stmts))
    assert got == [("OPTIONS", "/api/orders/filter"), ("POST", "/api/orders/filter")]
