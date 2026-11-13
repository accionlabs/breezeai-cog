"""ASP.NET Web Forms with VB code-behind: the same page, page-method, navigation, control and
master records as C# (``webforms_common``), read from ``.aspx.vb`` / ``.ascx.vb`` / ``.master.vb``."""

from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records
from breezeai_cog.core import registry
from breezeai_cog.parsers.contract import validate
from breezeai_cog.parsers.vb.parser import VbParser
from breezeai_cog.parsers.vb_aspnet.parser import VbAspNetParser
from breezeai_cog.parsers.vb_webforms.parser import VbWebFormsParser
from breezeai_cog.schemas import FileRecord

ORDERS_ASPX = (
    '<%@ Page Language="VB" MasterPageFile="~/Site.master" CodeBehind="Orders.aspx.vb" '
    'Inherits="Shop.Orders" %>\n'
    '<%@ Register Src="~/Controls/Cart.ascx" TagPrefix="uc" TagName="Cart" %>\n'
    '<asp:Content ContentPlaceHolderID="Main" runat="server">\n'
    '  <uc:Cart ID="cart" runat="server" />\n'
    '  <asp:HyperLink runat="server" NavigateUrl="~/Help.aspx" />\n'
    "</asp:Content>\n"
)
ORDERS_VB = (
    "Imports System.Web.Services\n"
    "\n"
    "Public Class Orders\n"                                                    # line 3
    "    Inherits System.Web.UI.Page\n"
    "    Protected Sub Page_Load(sender As Object, e As EventArgs) Handles Me.Load\n"
    '        Dim extra = LoadControl("~/Controls/Extra.ascx")\n'
    "    End Sub\n"
    "    Protected Sub Save_Click(sender As Object, e As EventArgs)\n"
    '        response.redirect("~/Confirm.aspx?id=1")\n'                       # line 9
    "    End Sub\n"
    "    <WebMethod()>\n"                                                      # line 11
    "    Public Shared Function Total(id As Integer) As Decimal\n"
    "        Return 0\n"
    "    End Function\n"
    "    <WebMethod()>\n"
    "    Public Function NotShared() As Decimal\n"                             # instance: no route
    "        Return 0\n"
    "    End Function\n"
    "End Class\n"
)


def _page(name: str) -> dict[str, str]:
    return {
        f"{name}.aspx": f'<%@ Page Language="VB" CodeBehind="{name}.aspx.vb" Inherits="Shop.{name}" %>\n',
        f"{name}.aspx.vb": f"Public Class {name}\n    Inherits System.Web.UI.Page\nEnd Class\n",
    }


SITE = {
    "web.config": "<configuration/>\n",
    "Orders.aspx": ORDERS_ASPX,
    "Orders.aspx.vb": ORDERS_VB,
    "Site.master": '<%@ Master Language="VB" CodeBehind="Site.master.vb" Inherits="Shop.SiteMaster" %>\n',
    "Site.master.vb": "Public Class SiteMaster\n    Inherits System.Web.UI.MasterPage\nEnd Class\n",
    "Controls/Cart.ascx": '<%@ Control Language="VB" CodeBehind="Cart.ascx.vb" Inherits="Shop.Cart" %>\n',
    "Controls/Cart.ascx.vb": "Public Class Cart\n    Inherits System.Web.UI.UserControl\nEnd Class\n",
    "Controls/Extra.ascx": '<%@ Control Language="VB" CodeBehind="Extra.ascx.vb" Inherits="Shop.Extra" %>\n',
    "Controls/Extra.ascx.vb": "Public Class Extra\n    Inherits System.Web.UI.UserControl\nEnd Class\n",
    **_page("Help"),
    **_page("Confirm"),
}


def _records(tmp_path: Path, files: dict[str, str]) -> dict[str, FileRecord]:
    repo = tmp_path / "repo"
    for rel, src in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    records = {r.path: r for r in iter_file_records(repo, capture_templates=True)}
    errors = [v for r in records.values() for v in validate(r) if v.severity == "error"]
    assert not errors, f"entry-point contract errors: {errors}"
    return records


def _routes(record: FileRecord) -> set[tuple]:
    return {(s.routeKind, s.method, s.endpoint, s.handler, s.handlerLine, s.nodeType)
            for s in record.statements if s.semanticType == "route"}


def test_vb_code_behind_is_claimed_by_suffix() -> None:
    registry.clear()
    for p in (VbParser(), VbAspNetParser(), VbWebFormsParser()):
        registry.register(p)
    assert registry.select("Orders.aspx.vb", b"Public Class Orders\nEnd Class\n").name == "vb-webforms"
    for path in ("Cart.ascx.vb", "Site.master.vb"):
        assert registry.select(path, b"").name == "vb-webforms"
    # The designer file and a plain class are not code-behind; System.Web.UI alone claims nothing.
    assert registry.select("Orders.aspx.designer.vb", b"").name == "vb"
    assert registry.select("Helper.vb", b"Imports System.Web.UI\n").name == "vb"
    assert registry.select("OrdersController.vb", b"Imports System.Web.Mvc\n").name == "vb-aspnet"


def test_a_vb_page_gets_the_records_a_csharp_page_gets(tmp_path: Path) -> None:
    recs = _records(tmp_path, SITE)
    orders = recs["Orders.aspx.vb"]
    assert orders.framework == "aspnet-webforms"
    assert _routes(orders) == {
        ("page", "GET", "/Orders.aspx", "Orders", 3, "synthetic"),
        ("route", "POST", "/Orders.aspx/Total", "Total", 11, "synthetic"),
        # VB is case-insensitive: response.redirect is Response.Redirect
        ("navigation", None, "/Confirm.aspx", "Orders", None, "invocation"),
        ("navigation", None, "/Help.aspx", "Orders", None, "synthetic"),
    }
    # The controls it mounts (markup Register + LoadControl) and its master, as code-behind files.
    assert orders.importFiles == [
        "Controls/Cart.ascx.vb", "Controls/Extra.ascx.vb", "Site.master.vb"]
    assert _routes(recs["Help.aspx.vb"]) == {
        ("page", "GET", "/Help.aspx", "Help", 1, "synthetic")}
    # A user control and a master page are parts of pages: Web Forms code, no route.
    for path in ("Controls/Cart.ascx.vb", "Site.master.vb"):
        assert recs[path].framework == "aspnet-webforms" and not _routes(recs[path])
    # The markup links to its VB code-behind.
    assert recs["Orders.aspx"].importFiles[0] == "Orders.aspx.vb"


def test_a_vb_mappageroute_gives_the_page_its_url(tmp_path: Path) -> None:
    recs = _records(tmp_path, {
        **SITE,
        "App_Start/RouteConfig.vb": (
            "Imports System.Web.Routing\n"
            "Public Module RouteConfig\n"
            "    Public Sub RegisterRoutes(routes As RouteCollection)\n"
            '        routes.MapPageRoute("orders", "shop/orders", "~/Orders.aspx")\n'
            '        routes.MapPageRoute("help", routeUrl:="shop/help", physicalFile:="~/Help.aspx")\n'
            "    End Sub\n"
            "End Module\n"
        ),
    })
    endpoints = {(s.routeKind, s.endpoint) for s in recs["Orders.aspx.vb"].statements
                 if s.semanticType == "route" and s.routeKind != "navigation"}
    assert endpoints == {("page", "/shop/orders"), ("route", "/shop/orders/Total")}
    # Named arguments are not read: the page keeps its physical address (missing, not wrong).
    assert {s.endpoint for s in recs["Help.aspx.vb"].statements
            if s.routeKind == "page"} == {"/Help.aspx"}


def test_only_literal_redirects_on_response_or_server_navigate(tmp_path: Path) -> None:
    recs = _records(tmp_path, {
        **_page("Confirm"),
        "Go.aspx": '<%@ Page Language="VB" CodeBehind="Go.aspx.vb" Inherits="Shop.Go" %>\n',
        "Go.aspx.vb": (
            "Public Class Go\n"
            "    Inherits System.Web.UI.Page\n"
            "    Sub A(target As String)\n"
            "        Response.Redirect(target)\n"                               # a variable
            '        Router.Redirect("~/Confirm.aspx")\n'                       # not Response
            '        Response.Redirect("https://example.com/x.aspx")\n'         # external
            '        HttpContext.Current.Server.Transfer("~/" & "Confirm.aspx")\n'
            "    End Sub\n"
            "End Class\n"
        ),
    })
    navs = [(s.endpoint, s.startLine) for s in recs["Go.aspx.vb"].statements
            if s.routeKind == "navigation"]
    assert navs == [("/Confirm.aspx", 7)]
