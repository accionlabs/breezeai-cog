"""Role-table tests for ASP.NET Web Forms (docs/entry-point-roles/webforms.md)."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

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
    return [s for s in stmts if is_entry_point(s)]


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


def test_aspx_page_has_handler_line(site) -> None:
    [page] = [s for s in _eps(site) if s.routeKind == "page"]
    assert page.handlerLine


def test_map_page_route_is_wiring(analyze_files) -> None:
    stmts = analyze_files(_site(global_asax=True), templates=True)
    assert [s for s in _eps(stmts) if s.path == "Global.asax.cs"] == []
    [page] = [s for s in _eps(stmts) if s.routeKind == "page"]
    assert page.endpoint == "/shop/orders"


def test_user_control_is_none(site) -> None:
    assert [s for s in _eps(site) if s.path == "Controls/Nav.ascx.cs"] == []


def test_register_src_is_wiring(site) -> None:
    assert [s for s in _eps(site) if s.path == "Orders.aspx"] == []


def test_register_src_keeps_structural_link(site, tmp_path) -> None:
    records = {r.path: r for r in _records(tmp_path)}
    assert "Controls/Nav.ascx.cs" in records["Orders.aspx.cs"].importFiles


def test_master_page_file_is_wiring(site) -> None:
    assert [s for s in _eps(site) if s.routeKind == "layout"] == []


def test_master_page_file_keeps_structural_link(site, tmp_path) -> None:
    records = {r.path: r for r in _records(tmp_path)}
    assert "Site.master.cs" in records["Orders.aspx.cs"].importFiles


def test_master_page_is_none(site) -> None:
    assert [s for s in _eps(site) if s.path == "Site.master.cs"] == []


def test_server_control_event_is_none(site) -> None:
    on_click = [s for s in site if s.path == "Orders.aspx" and s.handler == "Save_Click"]
    assert on_click and not any(is_entry_point(s) for s in on_click)


def test_redirect_is_outbound_navigation(site) -> None:
    nav = [s for s in site if s.endpoint == "/Done.aspx"]
    assert [(s.semanticType, s.routeKind) for s in nav] == [("route", "navigation")]


def test_navigate_url_is_outbound_navigation(site) -> None:
    nav = [s for s in site if s.endpoint == "/Help.aspx"]
    assert [(s.semanticType, s.routeKind) for s in nav] == [("route", "navigation")]


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


# --- handlers registered in web.config --------------------------------------------------------

ADMIN_CONFIG = """<?xml version="1.0"?>
<configuration xmlns="http://schemas.microsoft.com/.NetConfiguration/v2.0">
  <system.web>
    <httpHandlers>
      <add verb="GET,POST" path="Export.axd" type="Shop.Export, Shop" />
    </httpHandlers>
  </system.web>
  <location path="Api">
    <system.web>
      <httpHandlers><add verb="*" path="*.ashx" type="Shop.Api.Dispatcher, Shop" /></httpHandlers>
    </system.web>
    <system.webServer>
      <handlers><add name="Api" verb="*" path="*.ashx" type="Shop.Api.Dispatcher, Shop" /></handlers>
    </system.webServer>
  </location>
  <system.webServer>
    <handlers><add name="Static" verb="*" path="*.txt" type="System.Web.StaticFileHandler" /></handlers>
  </system.webServer>
</configuration>
"""

EXPORT = """namespace Shop
{
    public class Export : HandlerBase
    {
        public void ProcessRequest(System.Web.HttpContext context) { }
    }
}
"""

DISPATCHER = """using System.Web;

namespace Shop.Api
{
    public class Dispatcher : IHttpHandler
    {
        public bool IsReusable => true;
        public void ProcessRequest(HttpContext context) { }
    }
}
"""


def _registered(analyze_files, extra: dict[str, str] | None = None):
    files = {
        "web.config": "<configuration />",
        "Admin/web.config": ADMIN_CONFIG,
        "Shop/Export.cs": EXPORT,
        "Shop/Dispatcher.cs": DISPATCHER,
        **(extra or {}),
    }
    return sorted((s.path, s.method, s.endpoint, s.handler, s.handlerLine, s.nodeType)
                  for s in _eps(analyze_files(files)))


def test_web_config_handler_is_binding(analyze_files) -> None:
    # Folder under the application root + <location path> + path; a verb list is one record
    # per verb; the same mapping under httpHandlers and handlers is one record.
    assert _registered(analyze_files) == [
        ("Shop/Dispatcher.cs", "ANY", "/Admin/Api/*.ashx", "ProcessRequest", 8, "synthetic"),
        ("Shop/Export.cs", "GET", "/Admin/Export.axd", "ProcessRequest", 5, "synthetic"),
        ("Shop/Export.cs", "POST", "/Admin/Export.axd", "ProcessRequest", 5, "synthetic"),
    ]


def test_web_config_handler_needs_its_full_type_name(analyze_files) -> None:
    other = "namespace Other { public class Export { public void ProcessRequest(object c) { } } }"
    eps = _registered(analyze_files, {"Other/Export.cs": other})
    assert [e for e in eps if e[0] == "Other/Export.cs"] == []


def test_web_config_handler_without_entry_method_has_no_handler(analyze_files) -> None:
    files = {
        "web.config": """<configuration><system.web><httpHandlers>
  <add verb="*" path="feed.axd" type="Shop.Feed, Shop" />
</httpHandlers></system.web></configuration>""",
        "Feed.cs": "namespace Shop { public class Feed : FeedBase { } }",
    }
    [r] = _eps(analyze_files(files))
    assert (r.endpoint, r.method, r.handler, r.handlerLine) == ("/feed.axd", "ANY", None, None)


def test_web_config_transform_is_not_read(analyze_files) -> None:
    files = {
        "web.config": "<configuration />",
        "Web.Release.config": ADMIN_CONFIG,
        "Shop/Export.cs": EXPORT,
    }
    assert _eps(analyze_files(files)) == []


def test_malformed_web_config_gives_nothing(analyze_files) -> None:
    files = {
        "web.config": '<configuration><system.web><httpHandlers><add verb="*" path="x.axd" '
                      'type="Shop.Export, Shop"></httpHandlers>',
        "Shop/Export.cs": EXPORT,
    }
    assert _eps(analyze_files(files)) == []


# --- <%@ Register %> --------------------------------------------------------------------------

def test_register_directive_is_a_synthetic_statement(site) -> None:
    [reg] = [s for s in site if s.path == "Orders.aspx" and s.name == "Register"]
    assert (reg.nodeType, reg.startLine, reg.semanticType) == ("synthetic", 2, None)
    assert reg.text.startswith('<%@ Register TagPrefix="uc" TagName="Nav"')


def test_registered_tag_leads_to_its_control(site) -> None:
    [tag] = [s for s in site if s.path == "Orders.aspx" and s.name == "uc:Nav"]
    assert (tag.nodeType, tag.handler, tag.handlerPath) == ("element", "Nav", "Controls/Nav.ascx")


def test_registered_tag_matches_case_insensitively(analyze_files) -> None:
    files = {**_site(), "Orders.aspx": PAGE_MARKUP.replace("<uc:Nav", "<UC:nav")}
    stmts = analyze_files(files, templates=True)
    [tag] = [s for s in stmts if s.path == "Orders.aspx" and s.name == "UC:nav"]
    assert (tag.handler, tag.handlerPath) == ("Nav", "Controls/Nav.ascx")


def test_assembly_register_gives_the_statement_only(analyze_files) -> None:
    markup = ('<%@ Register TagPrefix="ajax" Namespace="Ajax.Kit" Assembly="Ajax" %>\n'
              '<ajax:Calendar runat="server" />\n')
    stmts = analyze_files({"web.config": "<configuration />", "Page.aspx": markup},
                          templates=True)
    assert [(s.name, s.nodeType, s.handler, s.handlerPath) for s in stmts] == [
        ("Register", "synthetic", None, None),
        ("ajax:Calendar", "element", None, None),
    ]


def test_register_of_a_missing_control_has_no_handler(analyze_files) -> None:
    markup = ('<%@ Register TagPrefix="uc" TagName="Gone" Src="~/Controls/Gone.ascx" %>\n'
              '<uc:Gone runat="server" />\n')
    stmts = analyze_files({"web.config": "<configuration />", "Page.aspx": markup},
                          templates=True)
    [tag] = [s for s in stmts if s.name == "uc:Gone"]
    assert (tag.handler, tag.handlerPath) == (None, None)
