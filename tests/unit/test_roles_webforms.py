"""Role-table tests for ASP.NET Web Forms (docs/entry-point-roles/webforms.md)."""

from __future__ import annotations

import pytest

from breezeai_cog import iter_file_records

EP = {"route", "eventbus_consumer", "timer"}

PAGE_CB = """using System;
using System.Web.UI;

namespace Shop
{
    public partial class Orders : Page
    {
        protected void Page_Load(object sender, EventArgs e) { }
        protected void Save_Click(object sender, EventArgs e)
        {
            Response.Redirect("~/Done.aspx");
        }
    }
}
"""

PAGE_MARKUP = """<%@ Page Language="C#" MasterPageFile="~/Site.master" CodeBehind="Orders.aspx.cs" Inherits="Shop.Orders" %>
<%@ Register TagPrefix="uc" TagName="Nav" Src="~/Controls/Nav.ascx" %>
<asp:Content ContentPlaceHolderID="Main" runat="server">
  <uc:Nav runat="server" />
  <asp:Button ID="Save" runat="server" OnClick="Save_Click" />
  <asp:HyperLink runat="server" NavigateUrl="~/Help.aspx" />
</asp:Content>
"""

CONTROL_CB = """using System.Web.UI;

namespace Shop.Controls
{
    public partial class Nav : UserControl { }
}
"""

MASTER_CB = """using System.Web.UI;

namespace Shop
{
    public partial class Site : MasterPage { }
}
"""

GLOBAL = """using System;
using System.Web;
using System.Web.Routing;

public class Global : HttpApplication
{
    protected void Application_Start(object sender, EventArgs e)
    {
        RouteTable.Routes.MapPageRoute("orders", "shop/orders", "~/Orders.aspx");
    }
}
"""

HANDLER = """using System.Web;

public class Export : IHttpHandler
{
    public bool IsReusable => false;
    public void ProcessRequest(HttpContext context) { }
}
"""


def _eps(stmts):
    return [s for s in stmts if s.semanticType in EP and s.routeKind != "field_resolver"]


def _site(global_asax: bool = False) -> dict[str, str]:
    files = {
        "web.config": "<configuration />",
        "Orders.aspx": PAGE_MARKUP,
        "Orders.aspx.cs": PAGE_CB,
        "Controls/Nav.ascx": '<%@ Control CodeBehind="Nav.ascx.cs" Inherits="Shop.Controls.Nav" %>',
        "Controls/Nav.ascx.cs": CONTROL_CB,
        "Site.master": '<%@ Master CodeBehind="Site.master.cs" Inherits="Shop.Site" %>',
        "Site.master.cs": MASTER_CB,
        "Done.aspx": "<html/>",
        "Help.aspx": "<html/>",
    }
    if global_asax:
        files["Global.asax.cs"] = GLOBAL
    return files


def _records(tmp_path):
    """File records of the repo the ``site`` fixture wrote (``analyze_files`` uses tmp_path/repo)."""
    return list(iter_file_records(tmp_path / "repo", capture_templates=True))


@pytest.fixture
def site(analyze_files):
    return analyze_files(_site(), templates=True)


def test_aspx_page_is_binding(site) -> None:
    pages = [s for s in _eps(site) if s.routeKind == "page"]
    assert [(p.path, p.method, p.endpoint, p.framework) for p in pages] == [
        ("Orders.aspx.cs", "GET", "/Orders.aspx", "aspnet-webforms")
    ]
    assert pages[0].handler == "Orders"


@pytest.mark.xfail(strict=True, reason="page record has no handlerLine")
def test_aspx_page_has_handler_line(site) -> None:
    [page] = [s for s in _eps(site) if s.routeKind == "page"]
    assert page.handlerLine


def test_map_page_route_is_wiring(analyze_files) -> None:
    stmts = analyze_files(_site(global_asax=True), templates=True)
    assert [s for s in _eps(stmts) if s.path == "Global.asax.cs"] == []
    [page] = [s for s in _eps(stmts) if s.routeKind == "page"]
    assert page.endpoint == "/shop/orders"


@pytest.mark.xfail(strict=True, reason="user control recorded as a route (routeKind mount)")
def test_user_control_is_none(site) -> None:
    assert [s for s in _eps(site) if s.path == "Controls/Nav.ascx.cs"] == []


@pytest.mark.xfail(strict=True, reason="Register Src recorded as a route (routeKind mount)")
def test_register_src_is_wiring(site) -> None:
    assert [s for s in _eps(site) if s.path == "Orders.aspx"] == []


def test_register_src_keeps_structural_link(site, tmp_path) -> None:
    records = {r.path: r for r in _records(tmp_path)}
    assert "Controls/Nav.ascx.cs" in records["Orders.aspx.cs"].importFiles


@pytest.mark.xfail(strict=True, reason="master page link recorded as a route (routeKind layout)")
def test_master_page_file_is_wiring(site) -> None:
    assert [s for s in _eps(site) if s.routeKind == "layout"] == []


def test_master_page_file_keeps_structural_link(site, tmp_path) -> None:
    records = {r.path: r for r in _records(tmp_path)}
    assert "Site.master.cs" in records["Orders.aspx.cs"].importFiles


def test_master_page_is_none(site) -> None:
    assert [s for s in _eps(site) if s.path == "Site.master.cs"] == []


def test_server_control_event_is_none(site) -> None:
    on_click = [s for s in site if s.path == "Orders.aspx" and s.handler == "Save_Click"]
    assert on_click and all(s.semanticType not in EP for s in on_click)


@pytest.mark.xfail(strict=True, reason="redirect recorded as route/navigation, not navigation")
def test_redirect_is_outbound_navigation(site) -> None:
    nav = [s for s in site if s.endpoint == "/Done.aspx"]
    assert [s.semanticType for s in nav] == ["navigation"]


@pytest.mark.xfail(strict=True, reason="link recorded as route/navigation, not navigation")
def test_navigate_url_is_outbound_navigation(site) -> None:
    nav = [s for s in site if s.endpoint == "/Help.aspx"]
    assert [s.semanticType for s in nav] == ["navigation"]


def test_application_start_is_none(analyze_files) -> None:
    stmts = analyze_files({"Global.asax.cs": GLOBAL})
    assert _eps(stmts) == []


def test_generic_handler_is_binding(analyze_files) -> None:
    stmts = analyze_files(
        {
            "Export.ashx": '<%@ WebHandler Language="C#" CodeBehind="Export.ashx.cs" Class="Export" %>',
            "Export.ashx.cs": HANDLER,
        }
    )
    [r] = _eps(stmts)
    assert (r.semanticType, r.endpoint, r.handler) == ("route", "/Export.ashx", "ProcessRequest")
