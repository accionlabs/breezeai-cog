"""ASP.NET Web Forms parser (C#): page/control route detection (convention-based,
file-parented), capture-gating, master/base-class skipping, and parser selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.csharp_aspnet.parser import AspNetCoreParser
from breezeai_cog.parsers.webforms_common.page_routes import page_routes
from breezeai_cog.parsers.emits import apply
from breezeai_cog.parsers.treesitter import parse_source
from breezeai_cog.parsers.csharp_webforms.parser import WebFormsParser
from breezeai_cog.parsers.webforms_common.pages import (
    detect_webforms_pages,
)
from breezeai_cog.schemas import FileRecord

PAGE = b"""
using System;
using System.Web.UI;
namespace Acme {
  public partial class Enrollment : Page {
    protected void Page_Load(object sender, EventArgs e) { }
  }
}
"""

CONTROL = b"""
using System;
using System.Web.UI;
namespace Acme {
  public partial class ButtonNavigation : UserControl {
    protected void Page_Load(object sender, EventArgs e) { }
  }
}
"""

MVC = b"""
using Microsoft.AspNetCore.Mvc;
namespace Acme {
  [ApiController]
  [Route("api/orders")]
  public class OrdersController : ControllerBase {
    [HttpGet] public object Get() { return null; }
  }
}
"""


def _parse(parser, src, name, *, capture=True) -> FileRecord:
    ctx = ParseContext(
        path=name, abs_path=None, source=src, repo_root=None, capture_statements=capture
    )
    return parser.parse_file(ctx)


def _parse_repo(
    parser, root: Path, target: str, files: dict[str, bytes], *, capture=True
) -> FileRecord:
    """Write ``files`` under ``root`` and parse ``target`` with on-disk context (so the
    markup pass can read the sibling markup + verify control code-behind targets)."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    ctx = ParseContext(
        path=target,
        abs_path=root / target,
        source=(root / target).read_bytes(),
        repo_root=root,
        capture_statements=capture,
    )
    return parser.parse_file(ctx)


def test_routes_require_capture() -> None:
    rec = _parse(WebFormsParser(), PAGE, "CMS/Enrollment.aspx.cs", capture=False)
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_page_route() -> None:
    rec = _parse(WebFormsParser(), PAGE, "CMS/Enrollment.aspx.cs")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    r = routes[0]
    assert r.routeKind == "page"
    assert r.nodeType == "synthetic"  # normalized synthetic marker (no backing AST node)
    assert r.framework == "aspnet-webforms"
    assert r.method == "GET"
    assert r.endpoint == "/CMS/Enrollment.aspx"
    assert r.handler == "Enrollment"  # code-behind class = markup stem
    assert r.parentId == "CMS/Enrollment.aspx.cs"  # file-parented (like React)
    assert rec.framework == "aspnet-webforms"


def test_user_control_has_no_record() -> None:
    # A user control is part of the pages that load it, not an entry point (contract §4).
    rec = _parse(WebFormsParser(), CONTROL, "CMS/Controls/ButtonNavigation.ascx.cs")
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework == "aspnet-webforms"


def test_master_page_skipped() -> None:
    # A .master.cs is a layout, not a route: claimed & parsed, but emits no route.
    rec = _parse(WebFormsParser(), PAGE, "CMS/Site.master.cs")
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


def test_base_class_file_has_no_route() -> None:
    # A shared base class (System.Web.UI import but NOT an .aspx.cs/.ascx.cs) is claimed
    # for structure yet is not itself a page/control → no route.
    src = b"using System.Web.UI;\nnamespace X { public class CMSBaseUserControl : UserControl {} }"
    rec = _parse(WebFormsParser(), src, "CMS/Code/CMSBaseUserControl.cs")
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert rec.framework is None


CTRL_CB = b"using System.Web.UI;\nnamespace Acme { public partial class Nav : UserControl {} }\n"


def test_mount_register_src(tmp_path: Path) -> None:
    # `<%@ Register Src %>` in the sibling markup → IMPORTS edge to the control's code-behind.
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "CMS/Page.aspx.cs",
        {
            "web.config": b"<configuration/>",  # app root = repo root
            "CMS/Page.aspx": b'<%@ Register TagName="Nav" Src="~/CMS/Controls/Nav.ascx" %>\n<html/>',
            "CMS/Page.aspx.cs": PAGE,
            "CMS/Controls/Nav.ascx.cs": CTRL_CB,  # target exists
        },
    )
    assert "CMS/Controls/Nav.ascx.cs" in rec.importFiles
    assert rec.framework == "aspnet-webforms"


def test_mount_loadcontrol_literal(tmp_path: Path) -> None:
    # `LoadControl("~/…")` in the code-behind resolves the same way.
    cb = (
        b"using System.Web.UI;\nnamespace Acme { public partial class P : Page {"
        b' void L(){ LoadControl("~/Controls/Cart.ascx"); } } }\n'
    )
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "Shop/P.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "Shop/P.aspx.cs": cb,
            "Controls/Cart.ascx.cs": CTRL_CB,
        },
    )
    assert "Controls/Cart.ascx.cs" in rec.importFiles


def test_mount_dynamic_loadcontrol_unresolved(tmp_path: Path) -> None:
    # A data-driven control name (no string literal) is honest-null — no edge.
    cb = (
        b"using System.Web.UI;\nnamespace Acme { public partial class P : Page {"
        b" void L(string n){ LoadControl(n); } } }\n"
    )
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "P.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "P.aspx.cs": cb,
        },
    )
    assert rec.importFiles == []


def test_mount_missing_codebehind_skipped(tmp_path: Path) -> None:
    # Registered control whose .ascx.cs does not exist (inline-code control) → no dangling edge.
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "Page.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "Page.aspx": b'<%@ Register Src="~/Controls/Inline.ascx" %>',
            "Page.aspx.cs": PAGE,  # Controls/Inline.ascx.cs intentionally absent
        },
    )
    assert rec.importFiles == []


def test_mount_app_root_in_subdir(tmp_path: Path) -> None:
    # `~/` resolves against the app root (nearest web.config), not the repo root.
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "App/CMS/Page.aspx.cs",
        {
            "App/web.config": b"<configuration/>",  # app root = App/
            "App/CMS/Page.aspx": b'<%@ Register Src="~/Controls/Nav.ascx" %>',
            "App/CMS/Page.aspx.cs": PAGE,
            "App/Controls/Nav.ascx.cs": CTRL_CB,  # ~/ → App/Controls/…
        },
    )
    assert "App/Controls/Nav.ascx.cs" in rec.importFiles


def test_mount_requires_disk_context() -> None:
    # In-memory parse (no repo_root/abs_path) must not crash and yields no mounts.
    rec = _parse(WebFormsParser(), PAGE, "CMS/Page.aspx.cs")
    assert rec.importFiles == []


def _page_routes(*cs_sources: bytes) -> dict[str, list[str]]:
    """Build the C# index over given RouteConfig-style sources and return page_routes."""
    import tempfile
    from breezeai_cog.parsers.csharp.imports import build_csharp_index

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        files = []
        for i, src in enumerate(cs_sources):
            p = root / f"App_Start/RouteConfig{i}.cs"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(src)
            files.append(p)
        return page_routes(build_csharp_index(root, files))


ROUTECFG = b"""
using System.Web.Routing;
namespace Acme {
  public static class RouteConfig {
    public static void Register(RouteCollection routes) {
      routes.MapPageRoute("enroll", "enroll/{id}", "~/CMS/Enrollment.aspx");
    }
  }
}
"""


def test_mappageroute_index_extracts_literal() -> None:
    pr = _page_routes(ROUTECFG)
    assert pr == {"CMS/Enrollment.aspx": ["enroll/{id}"]}


def test_mappageroute_subdir_app_root() -> None:
    # When the web app lives in a subdirectory of the repo (e.g. SplendidCRM-CE/SplendidCRM/),
    # ~/CMS/Enrollment.aspx must resolve to the full repo-relative key so that
    # detect_webforms_pages can match it against the code-behind file's path.
    import tempfile
    from breezeai_cog.parsers.csharp.imports import build_csharp_index

    src = b"""
using System.Web.Routing;
namespace Acme {
  public static class RouteConfig {
    public static void Register(RouteCollection routes) {
      routes.MapPageRoute("enroll", "enroll/{id}", "~/CMS/Enrollment.aspx");
    }
  }
}
"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        # App lives under "SplendidCRM-CE/App" — place web.config there so _aspx_app_root
        # identifies it as the app root.
        app = root / "SplendidCRM-CE" / "App"
        app.mkdir(parents=True)
        (app / "web.config").write_bytes(b"<configuration/>")
        cfg = app / "App_Start" / "RouteConfig.cs"
        cfg.parent.mkdir(parents=True)
        cfg.write_bytes(src)
        pr = page_routes(build_csharp_index(root, [cfg]))
    # Key must include the full subdir prefix so it matches
    # "SplendidCRM-CE/App/CMS/Enrollment.aspx.cs"[:-len(".cs")]
    assert pr == {"SplendidCRM-CE/App/CMS/Enrollment.aspx": ["enroll/{id}"]}


def test_mappageroute_app_root_does_not_depend_on_path_spelling(tmp_path: Path, windows_spelled) -> None:
    # A Windows path's string never ends with its "/"-separated repo-relative path; the repo
    # root must still reach the collector, so the subdir app root is found.
    from breezeai_cog.parsers.csharp.imports import build_csharp_index

    app = tmp_path / "Shop"
    (app / "App_Start").mkdir(parents=True)
    (app / "web.config").write_bytes(b"<configuration/>")
    (app / "App_Start" / "RouteConfig.cs").write_bytes(ROUTECFG)
    cfg = windows_spelled(app / "App_Start" / "RouteConfig.cs")
    assert page_routes(build_csharp_index(tmp_path, [cfg])) == {
        "Shop/CMS/Enrollment.aspx": ["enroll/{id}"]}


def test_mappageroute_ast_only_ignores_comment_and_string() -> None:
    # A MapPageRoute in a comment or string must NOT be picked up (AST extraction, not regex).
    src = b"""
namespace X { class C {
  // routes.MapPageRoute("ghost", "ghost/{id}", "~/Ghost.aspx");
  string s = "routes.MapPageRoute(\\"s\\",\\"s\\",\\"~/S.aspx\\")";
} }
"""
    assert _page_routes(src) == {}


def test_mappageroute_dynamic_arg_unresolved() -> None:
    # Non-literal physical/url arg → honest-null, no mapping.
    src = b"""
namespace X { class C { void R(System.Web.Routing.RouteCollection routes, string phys) {
  routes.MapPageRoute("n", "url/{id}", phys);
} } }
"""
    assert _page_routes(src) == {}


def _pages(rec, path: str, page_routes_: dict[str, list[str]]) -> list:
    """The records ``detect_webforms_pages`` writes onto ``rec`` (through ``apply``)."""
    root = parse_source("csharp", b"", 0).root_node  # stands in for the node, as in the parser
    before = len(rec.statements)
    apply(rec, detect_webforms_pages(rec, root, path, page_routes_), b"", path)
    return rec.statements[before:]


def test_page_route_uses_friendly_url() -> None:
    rec = _parse(WebFormsParser(), PAGE, "CMS/Enrollment.aspx.cs")
    routes = _pages(rec, "CMS/Enrollment.aspx.cs", {"CMS/Enrollment.aspx": ["enroll/{id}"]})
    assert len(routes) == 1
    assert routes[0].endpoint == "/enroll/{id}"  # friendly, not /CMS/Enrollment.aspx
    assert routes[0].routeKind == "page"


def test_page_route_subdir_app_end_to_end(tmp_path: Path) -> None:
    # Full pipeline: RouteConfig.cs in a subdir app produces a key that matches the
    # code-behind's full repo-relative path, so the page gets the friendly endpoint.
    from breezeai_cog.parsers.csharp.imports import build_csharp_index

    route_src = b"""
using System.Web.Routing;
namespace Acme {
  public static class RouteConfig {
    public static void Register(RouteCollection routes) {
      routes.MapPageRoute("enroll", "enroll/{id}", "~/CMS/Enrollment.aspx");
    }
  }
}
"""
    app = tmp_path / "SplendidCRM-CE" / "App"
    app.mkdir(parents=True)
    (app / "web.config").write_bytes(b"<configuration/>")
    cfg = app / "App_Start" / "RouteConfig.cs"
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(route_src)
    pr = page_routes(build_csharp_index(tmp_path, [cfg]))

    # Now simulate detect_webforms_pages for the code-behind at the full repo-relative path.
    page_path = "SplendidCRM-CE/App/CMS/Enrollment.aspx.cs"
    rec = _parse(WebFormsParser(), PAGE, page_path)
    routes = _pages(rec, page_path, pr)
    assert len(routes) == 1
    assert routes[0].endpoint == "/enroll/{id}"


def test_page_route_multiple_friendly_urls() -> None:
    routes = _pages(
        _parse(WebFormsParser(), PAGE, "P.aspx.cs"), "P.aspx.cs", {"P.aspx": ["a/{id}", "b/{id}"]}
    )
    assert sorted(r.endpoint for r in routes) == ["/a/{id}", "/b/{id}"]
    assert len({r.id for r in routes}) == 2  # distinct ids


def test_page_route_physical_fallback_without_mapping() -> None:
    routes = _pages(
        _parse(WebFormsParser(), PAGE, "CMS/Enrollment.aspx.cs"),
        "CMS/Enrollment.aspx.cs",
        {"Other.aspx": ["x"]},
    )
    assert routes[0].endpoint == "/CMS/Enrollment.aspx"  # no match → physical, as before


def test_control_not_friendly_routed() -> None:
    # A control never becomes a route, even if a MapPageRoute URL keys its path.
    routes = _pages(
        _parse(WebFormsParser(), CONTROL, "Ctrl.ascx.cs"),
        "Ctrl.ascx.cs",
        {"Ctrl.ascx": ["should/not/apply"]},
    )
    assert routes == []


MASTER_CB = b"using System.Web.UI;\nnamespace Acme { public partial class Site : MasterPage {} }\n"


def test_master_page_is_a_structural_link(tmp_path: Path) -> None:
    # `<%@ Page MasterPageFile %>` → the page→master link is an import edge, not a route record.
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "CMS/Enrollment.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "CMS/Enrollment.aspx": b'<%@ Page MasterPageFile="~/Site.master" %>\n<html/>',
            "CMS/Enrollment.aspx.cs": PAGE,
            "Site.master": b"<%@ Master %>",  # target must exist on disk
            "Site.master.cs": MASTER_CB,
        },
    )
    assert {s.routeKind for s in rec.statements if s.semanticType == "route"} == {"page"}
    assert "Site.master.cs" in rec.importFiles


def test_master_layout_nesting(tmp_path: Path) -> None:
    # A .master can itself declare a parent master (`<%@ Master MasterPageFile %>`).
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "Site.master.cs",
        {
            "web.config": b"<configuration/>",
            "Site.master": b'<%@ Master MasterPageFile="~/Root.master" %>',
            "Site.master.cs": MASTER_CB,
            "Root.master": b"<%@ Master %>",
            "Root.master.cs": MASTER_CB,
        },
    )
    assert [s for s in rec.statements if s.semanticType == "route"] == []
    assert "Root.master.cs" in rec.importFiles  # master → parent master edge


def test_master_import_edge(tmp_path: Path) -> None:
    # Page→master is a traversable IMPORTS edge (to the master's code-behind).
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "CMS/Enrollment.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "CMS/Enrollment.aspx": b'<%@ Page MasterPageFile="~/Site.master" %>',
            "CMS/Enrollment.aspx.cs": PAGE,
            "Site.master": b"<%@ Master %>",
            "Site.master.cs": MASTER_CB,
        },
    )
    assert "Site.master.cs" in rec.importFiles  # page→master IMPORTS edge


def test_master_inline_no_import_edge(tmp_path: Path) -> None:
    # Master with no code-behind (inline <script runat=server>) → no dangling IMPORTS edge
    # (honest-null); the markup file keeps its link to the .master itself.
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "P.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "P.aspx": b'<%@ Page MasterPageFile="~/Inline.master" %>',
            "P.aspx.cs": PAGE,
            "Inline.master": b"<%@ Master %>",  # no Inline.master.cs
        },
    )
    assert "Inline.master.cs" not in rec.importFiles  # no dangling edge


def test_master_missing_file_skipped(tmp_path: Path) -> None:
    # Declared master not present in the repo → honest-null, no edge.
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "P.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "P.aspx": b'<%@ Page MasterPageFile="~/Ghost.master" %>',
            "P.aspx.cs": PAGE,
        },
    )
    assert not any("Ghost.master" in i for i in rec.importFiles)


def test_master_directive_only_not_comment(tmp_path: Path) -> None:
    # MasterPageFile in an HTML comment is not a directive → not captured.
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "P.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "P.aspx": b'<!-- MasterPageFile="~/Site.master" -->\n<html/>',
            "P.aspx.cs": PAGE,
            "Site.master": b"x",
            "Site.master.cs": MASTER_CB,
        },
    )
    assert "Site.master.cs" not in rec.importFiles


def _nav_page(body: bytes) -> bytes:
    return (
        b"""
using System; using System.Web.UI;
namespace Acme { public partial class Nav1 : Page {
  protected void Submit(object s, EventArgs e) { """
        + body
        + b""" }
} }
"""
    )


def _navs(rec: FileRecord) -> list:
    return [s for s in rec.statements if s.routeKind == "navigation"]


def test_navigation_redirect_real_node() -> None:
    rec = _parse(
        WebFormsParser(),
        _nav_page(b'Response.Redirect("~/Login.aspx?ReturnUrl=x");'),
        "CMS/Nav1.aspx.cs",
    )
    navs = _navs(rec)
    assert len(navs) == 1
    assert navs[0].endpoint == "/Login.aspx"  # query stripped
    assert navs[0].nodeType == "invocation_expression"  # real call, not synthetic
    assert (navs[0].semanticType, navs[0].routeKind) == ("route", "navigation")


def test_navigation_server_transfer() -> None:
    rec = _parse(WebFormsParser(), _nav_page(b'Server.Transfer("~/Confirm.aspx");'), "P.aspx.cs")
    assert [s.endpoint for s in _navs(rec)] == ["/Confirm.aspx"]


def test_navigation_external_and_friendly_skipped() -> None:
    rec = _parse(
        WebFormsParser(),
        _nav_page(b'Response.Redirect("https://x.com/a"); Response.Redirect("~/enroll/5");'),
        "P.aspx.cs",
    )
    assert _navs(rec) == []  # external + friendly-URL (no .aspx) → none


def test_navigation_dynamic_and_wrong_receiver_skipped() -> None:
    rec = _parse(
        WebFormsParser(),
        _nav_page(b'Response.Redirect(url); helper.Transfer("~/X.aspx");'),
        "P.aspx.cs",
    )
    assert _navs(rec) == []  # non-literal + non-Response/Server receiver


def test_navigation_markup_attrs(tmp_path: Path) -> None:
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "CMS/Page.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "CMS/Page.aspx": b'<asp:HyperLink NavigateUrl="~/Help.aspx"/>'
            b'<asp:Button PostBackUrl="~/Submit.aspx"/>',
            "CMS/Page.aspx.cs": PAGE,
        },
    )
    navs = _navs(rec)
    assert sorted(s.endpoint for s in navs) == ["/Help.aspx", "/Submit.aspx"]
    assert all(s.nodeType == "synthetic" for s in navs)  # markup attrs → synthetic


def test_mount_case_insensitive(tmp_path: Path) -> None:
    # Web Forms path refs are routinely mis-cased vs disk (case-insensitive Windows origin).
    # `~/Controls/NAV.ascx` must resolve to the real `Nav.ascx.cs` (so the IMPORTS join lands).
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "Page.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "Page.aspx": b'<%@ Register Src="~/Controls/NAV.ascx" %>',  # uppercase in the ref
            "Page.aspx.cs": PAGE,
            "Controls/Nav.ascx.cs": CTRL_CB,  # real casing on disk
        },
    )
    assert "Controls/Nav.ascx.cs" in rec.importFiles  # real casing, not "NAV"


def test_master_case_insensitive(tmp_path: Path) -> None:
    # `~/Root.Master` (capital M in the ref) → the real `Root.master` (SplendidCRM's exact case bug).
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "P.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "P.aspx": b'<%@ Page MasterPageFile="~/Root.Master" %>',
            "P.aspx.cs": PAGE,
            "Root.master": b"<%@ Master %>",
            "Root.master.cs": MASTER_CB,
        },
    )
    assert "Root.master.cs" in rec.importFiles  # resolved to the real casing


def test_mount_case_collision_honest_null(tmp_path: Path) -> None:
    # Two controls differing only in case → the name is ambiguous → no mount (honest-null,
    # never an arbitrary pick). Only creatable on a case-sensitive filesystem.
    (tmp_path / "Controls").mkdir()
    (tmp_path / "Controls" / "Nav.ascx.cs").write_bytes(CTRL_CB)
    (tmp_path / "Controls" / "NAV.ascx.cs").write_bytes(CTRL_CB)
    if len(list((tmp_path / "Controls").iterdir())) < 2:
        import pytest

        pytest.skip("case-insensitive filesystem — collision not reproducible")
    rec = _parse_repo(
        WebFormsParser(),
        tmp_path,
        "Page.aspx.cs",
        {
            "web.config": b"<configuration/>",
            "Page.aspx": b'<%@ Register Src="~/Controls/nav.ascx" %>',
            "Page.aspx.cs": PAGE,
        },
    )
    assert [i for i in rec.importFiles if i.lower().endswith(".ascx.cs")] == []


def test_output_validates() -> None:
    for src, name in [(PAGE, "CMS/Enrollment.aspx.cs"), (CONTROL, "CMS/Ctrl.ascx.cs")]:
        rec = _parse(WebFormsParser(), src, name)
        errors = list(
            Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
                json.loads(to_line(rec))
            )
        )
        assert not errors, errors


def test_selection() -> None:
    registry.clear()
    for p in (CSharpParser(), AspNetCoreParser(), WebFormsParser()):
        registry.register(p)
    assert registry.select("CMS/Enrollment.aspx.cs", PAGE).name == "csharp-webforms"
    assert registry.select("CMS/Ctrl.ascx.cs", CONTROL).name == "csharp-webforms"
    assert registry.select("Orders.cs", MVC).name == "csharp-aspnet"  # disjoint claims
    assert registry.select("plain.cs", b"class C {}").name == "csharp"
    registry.clear()


# ── Step 1: .aspx/.ascx/.master markup pass (own the markup file) ──────────────

_ASPX_MARKUP = b"""<%@ Page CodeBehind="Enrollment.aspx.cs" Inherits="CMS.Enrollment" %>
<asp:Button ID="save" runat="server" Text="Save" OnClick="Save_Click" />
<asp:GridView ID="grid" runat="server" OnRowCommand="Grid_RowCommand">
  <ItemTemplate><asp:Label Text='<%# Eval("Title") %>' /></ItemTemplate>
</asp:GridView>
<div class="plain">no capture</div>"""


def _parse_markup(tmp_path, name: str, source: bytes, *, capture=True) -> FileRecord:
    (tmp_path / "Enrollment.aspx.cs").write_text("using System.Web.UI; class X {}")
    p = tmp_path / name
    p.write_bytes(source)
    ctx = ParseContext(
        path=name, abs_path=p, source=source, repo_root=tmp_path, capture_statements=capture
    )
    return WebFormsParser().parse_file(ctx)


def test_aspx_markup_is_a_file_node(tmp_path) -> None:
    rec = _parse_markup(tmp_path, "Enrollment.aspx", _ASPX_MARKUP)
    assert rec.language == "html"
    assert rec.framework == "aspnet-webforms"
    assert rec.uiRole == "template"
    assert rec.importFiles == ["Enrollment.aspx.cs"]  # markup → code-behind IMPORTS


def test_onclick_wiring_captures_handler(tmp_path) -> None:
    rec = _parse_markup(tmp_path, "Enrollment.aspx", _ASPX_MARKUP)
    wiring = {s.name: s.handler for s in rec.statements if s.nodeType == "attribute"}
    assert wiring["OnClick"] == "Save_Click"
    assert wiring["OnRowCommand"] == "Grid_RowCommand"


def test_server_controls_captured_plain_html_skipped(tmp_path) -> None:
    rec = _parse_markup(tmp_path, "Enrollment.aspx", _ASPX_MARKUP)
    controls = {s.name for s in rec.statements if s.nodeType == "element"}
    assert {"asp:Button", "asp:GridView", "asp:Label"} <= controls
    assert "div" not in controls  # plain HTML is not a server control → not captured


def test_markup_requires_capture(tmp_path) -> None:
    rec = _parse_markup(tmp_path, "Enrollment.aspx", _ASPX_MARKUP, capture=False)
    assert rec.uiRole == "template"  # File role is structural, not gated
    assert rec.statements == []  # markup statements are gated on statement capture


def test_markup_selected_over_base(tmp_path) -> None:
    registry.clear()
    registry.register(CSharpParser())
    registry.register(WebFormsParser())
    assert registry.select("Page.aspx", _ASPX_MARKUP).name == "csharp-webforms"
    assert ".aspx" in registry.capabilities()["extensions"]
    registry.clear()


def test_markup_output_validates(tmp_path) -> None:
    rec = _parse_markup(tmp_path, "Enrollment.aspx", _ASPX_MARKUP)
    errors = list(
        Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
            json.loads(to_line(rec))
        )
    )
    assert not errors, errors


# ── Step 2: host→control mount edges (765 Defect #1) ──────────────────────────


def _host_with_control(tmp_path):
    (tmp_path / "Controls").mkdir()
    (tmp_path / "Controls" / "Shortcuts.ascx").write_bytes(b"<asp:Panel runat='server'/>")
    (tmp_path / "Default.aspx.cs").write_text("using System.Web.UI; class X {}")
    aspx = (
        b'<%@ Page CodeBehind="Default.aspx.cs" %>\n'
        b'<%@ Register Src="~/Controls/Shortcuts.ascx" TagName="Shortcuts" TagPrefix="uc" %>\n'
        b'<uc:Shortcuts runat="server" ID="sc" />'
    )
    (tmp_path / "Default.aspx").write_bytes(aspx)
    ctx = ParseContext(
        path="Default.aspx",
        abs_path=tmp_path / "Default.aspx",
        source=aspx,
        repo_root=tmp_path,
        capture_statements=True,
    )
    return WebFormsParser().parse_file(ctx)


def test_host_to_control_import_edge(tmp_path) -> None:
    rec = _host_with_control(tmp_path)
    # host .aspx → control .ascx composition edge (IMPORTS), plus the code-behind
    assert "Controls/Shortcuts.ascx" in rec.importFiles
    assert "Default.aspx.cs" in rec.importFiles


def test_host_to_control_is_not_a_route(tmp_path) -> None:
    # The host → control composition is the import edge above, never a route record.
    rec = _host_with_control(tmp_path)
    assert [s for s in rec.statements if s.semanticType == "route"] == []


def test_mount_to_missing_control_is_dropped(tmp_path) -> None:
    # Register Src to a file that doesn't exist → no dangling edge (honest-null).
    (tmp_path / "Default.aspx.cs").write_text("using System.Web.UI; class X {}")
    aspx = b'<%@ Register Src="~/Controls/Ghost.ascx" TagPrefix="uc" TagName="G" %>\n<uc:G />'
    (tmp_path / "Default.aspx").write_bytes(aspx)
    ctx = ParseContext(
        path="Default.aspx",
        abs_path=tmp_path / "Default.aspx",
        source=aspx,
        repo_root=tmp_path,
        capture_statements=True,
    )
    rec = WebFormsParser().parse_file(ctx)
    assert not any("Ghost.ascx" in i for i in rec.importFiles)
    assert not any(s.routeKind == "mount" for s in rec.statements)


# ── Step 3: <%# Eval/Bind %> data-binding fields ──────────────────────────────


def _parse_databind(tmp_path, aspx: bytes):
    (tmp_path / "List.ascx.cs").write_text("using System.Web.UI; class X {}")
    (tmp_path / "List.ascx").write_bytes(aspx)
    ctx = ParseContext(
        path="List.ascx",
        abs_path=tmp_path / "List.ascx",
        source=aspx,
        repo_root=tmp_path,
        capture_statements=True,
    )
    return WebFormsParser().parse_file(ctx)


def test_databind_field_captured(tmp_path) -> None:
    aspx = (
        b'<%@ Control CodeBehind="List.ascx.cs" %>\n'
        b'<asp:Label runat="server" Text=\'<%# Eval("Title") %>\' />\n'
        b'<asp:TextBox runat="server" Text=\'<%# Bind("CustomerName") %>\' />'
    )
    rec = _parse_databind(tmp_path, aspx)
    binds = {s.name for s in rec.statements if s.nodeType == "attribute" and s.handler is None}
    assert "Title" in binds and "CustomerName" in binds
    # data bindings are ordinary markup — no semanticType (like Angular/Vue interpolations),
    # and a real captured node (attribute), never synthetic.
    for s in rec.statements:
        if s.name in {"Title", "CustomerName"}:
            assert s.semanticType is None and s.nodeType == "attribute"


def test_standalone_island_not_captured(tmp_path) -> None:
    # A <%# %> in a plain (non-server) element body has no clean node → not captured
    # (we don't emit synthetic+null). Honest-null gap.
    aspx = b'<%@ Control CodeBehind="List.ascx.cs" %>\n<td><%# Eval("Standalone") %></td>'
    rec = _parse_databind(tmp_path, aspx)
    assert not any(s.name == "Standalone" for s in rec.statements)


def test_databind_and_event_wiring_distinct(tmp_path) -> None:
    # Event wiring (handler set) and data binding (handler None) are both attribute nodes.
    aspx = (
        b'<%@ Control CodeBehind="List.ascx.cs" %>\n'
        b'<asp:Button runat="server" Text=\'<%# Eval("Caption") %>\' OnClick="Go" />'
    )
    rec = _parse_databind(tmp_path, aspx)
    attrs = {s.name: s.handler for s in rec.statements if s.nodeType == "attribute"}
    assert attrs.get("OnClick") == "Go"  # event wiring
    assert "Caption" in attrs and attrs["Caption"] is None  # data binding


def test_expression_binding_not_a_field(tmp_path) -> None:
    # A <%# %> binding that names no data field (visibility/localization expression) is view
    # logic, not a screen field → not captured as a data binding (no expression noise in name).
    aspx = (
        b'<%@ Control CodeBehind="List.ascx.cs" %>\n'
        b"<asp:Panel runat=\"server\" Visible='<%# !PrintView %>' />\n"
        b'<asp:Label runat="server" Text=\'<%# Eval("Real") %>\' />'
    )
    rec = _parse_databind(tmp_path, aspx)
    names = {s.name for s in rec.statements if s.nodeType == "attribute" and s.handler is None}
    assert "Real" in names  # the Eval field is captured
    assert not any(n and "PrintView" in n for n in names)  # the expression binding is not


# ── Step 4: markup → master-page composition edge ─────────────────────────────


def test_markup_links_to_master(tmp_path) -> None:
    (tmp_path / "DefaultView.master").write_bytes(b"<%@ Master %>")
    (tmp_path / "Enrollment.aspx.cs").write_text("using System.Web.UI; class X {}")
    aspx = (
        b'<%@ Page MasterPageFile="~/DefaultView.master" CodeBehind="Enrollment.aspx.cs" %>\n'
        b'<asp:Label runat="server" />'
    )
    (tmp_path / "Enrollment.aspx").write_bytes(aspx)
    ctx = ParseContext(
        path="Enrollment.aspx",
        abs_path=tmp_path / "Enrollment.aspx",
        source=aspx,
        repo_root=tmp_path,
        capture_statements=True,
    )
    rec = WebFormsParser().parse_file(ctx)
    assert "DefaultView.master" in rec.importFiles  # markup → master layout edge
    assert "Enrollment.aspx.cs" in rec.importFiles


def test_markup_master_missing_is_dropped(tmp_path) -> None:
    # MasterPageFile to a file that doesn't exist → no dangling edge.
    (tmp_path / "Enrollment.aspx.cs").write_text("using System.Web.UI; class X {}")
    aspx = b'<%@ Page MasterPageFile="~/Ghost.master" CodeBehind="Enrollment.aspx.cs" %>\n<div/>'
    (tmp_path / "Enrollment.aspx").write_bytes(aspx)
    ctx = ParseContext(
        path="Enrollment.aspx",
        abs_path=tmp_path / "Enrollment.aspx",
        source=aspx,
        repo_root=tmp_path,
        capture_statements=True,
    )
    rec = WebFormsParser().parse_file(ctx)
    assert not any("Ghost.master" in i for i in rec.importFiles)


def test_generic_handlers_are_bound_at_their_ashx(analyze_files) -> None:
    sync_cb = (
        "using System.Web;\nnamespace Shop {\npublic class Export : IHttpHandler\n{\n"
        "    public bool IsReusable => false;\n"
        "    public void ProcessRequest(HttpContext context) { }\n}\n}\n"
    )
    async_cb = (
        "using System.Web;\npublic class Feed : HttpTaskAsyncHandler\n{\n"
        "    public override async Task ProcessRequestAsync(HttpContext context) { }\n}\n"
    )
    stmts = analyze_files({
        "Handlers/Export.ashx": '<%@ WebHandler Language="C#" CodeBehind="Export.ashx.cs" Class="Shop.Export" %>',
        "Handlers/Export.ashx.cs": sync_cb,
        "Feed.ashx": '<%@ WebHandler Class="Feed" %>',
        "Feed.ashx.cs": async_cb,
        "Other.ashx": '<%@ WebHandler Class="Shop.Somewhere" %>',  # names another class
        "Other.ashx.cs": sync_cb.replace("Export", "Other"),
        "Loose.cs": sync_cb.replace("Export", "Loose"),               # no .ashx at all
    })
    got = {(s.path, s.method, s.endpoint, s.handler, s.handlerLine)
           for s in stmts if s.semanticType == "route"}
    assert got == {
        ("Handlers/Export.ashx.cs", "ANY", "/Handlers/Export.ashx", "ProcessRequest", 6),
        ("Feed.ashx.cs", "ANY", "/Feed.ashx", "ProcessRequestAsync", 4),
    }


def test_page_handler_line_and_page_methods(analyze_files) -> None:
    # Decision Q6: the page class is the handler (handlerLine = its declaration); each static
    # [WebMethod] page method is its own POST entry point at <page URL>/<Method>.
    st = analyze_files({
        "Orders/List.aspx": '<%@ Page Language="C#" CodeBehind="List.aspx.cs" Inherits="Shop.Orders.List" %>\n',
        "Orders/List.aspx.cs": (
            "using System.Web.Services;\nusing System.Web.UI;\nnamespace Shop.Orders {\n"
            "public partial class List : Page {\n"                          # line 4
            "  protected void Page_Load(object s, System.EventArgs e) { }\n"
            "  [WebMethod]\n"                                               # line 6
            "  public static string[] Search(string term) { return null; }\n"
            "  [WebMethod]\n"
            "  public string NotStatic() { return null; }\n"               # instance: not a page method
            "}\n}\n"
        ),
    })
    routes = {(s.method, s.endpoint): (s.handler, s.handlerLine, s.framework)
              for s in st if s.semanticType == "route" and s.path == "Orders/List.aspx.cs"}
    assert routes[("GET", "/Orders/List.aspx")] == ("List", 4, "aspnet-webforms")
    assert routes[("POST", "/Orders/List.aspx/Search")] == ("Search", 6, "aspnet-webforms")
    assert not any(ep and ep.endswith("NotStatic") for _, ep in routes)
    assert not [s for s in st if s.framework == "asmx"]  # not also an ASMX operation


# --- server code read as C#, page links and scripts (shared template readers) ----------------

_SERVER_CODE_PAGE = b"""<%@ Page Language="C#" CodeBehind="Enrollment.aspx.cs" Inherits="Acme.Enrollment" %>
<%-- <% if (commentedOut) { %> --%>
<asp:Label ID="Hello" runat="server" Text='<%$ Resources:Main, Hello %>' />
<% if (User.IsInRole("Admin")) { %> <a href="/Admin.aspx">Admin</a> <% } else { %> <p>no</p> <% } %>
<p><%= Title %></p><p><%# Eval("Id") %></p>
<script src="/Scripts/jquery.js"></script>
<script>$.post('/api/track');</script>
<script runat="server">
    void Page_Load(object sender, EventArgs e)
    {
        if (!IsPostBack) { Bind(); }
    }
</script>
"""


def test_server_code_if_is_an_if_statement_at_its_line(tmp_path) -> None:
    rec = _parse_markup(tmp_path, "Enrollment.aspx", _SERVER_CODE_PAGE)
    ifs = sorted((s.startLine, s.parentId) for s in rec.statements if s.nodeType == "if_statement")
    # the page-level <% if %> hangs off the page; the one in Page_Load off its method
    assert ifs == [(4, "Enrollment.aspx"), (11, "Enrollment.aspx#Page_Load@9")]
    assert not any("commentedOut" in s.text for s in rec.statements)  # a comment is not code


def test_server_script_methods_are_functions_of_the_page(tmp_path) -> None:
    rec = _parse_markup(tmp_path, "Enrollment.aspx", _SERVER_CODE_PAGE)
    assert [(f.name, f.parentId, f.startLine, f.endLine) for f in rec.functions] == [
        ("Page_Load", "Enrollment.aspx", 9, 12)]
    # structure, not statements: the function is there without statement capture too
    off = _parse_markup(tmp_path, "Enrollment.aspx", _SERVER_CODE_PAGE, capture=False)
    assert [f.name for f in off.functions] == ["Page_Load"] and off.statements == []


def test_server_expressions_cite_the_page_text(tmp_path) -> None:
    rec = _parse_markup(tmp_path, "Enrollment.aspx", _SERVER_CODE_PAGE)
    texts = {s.text for s in rec.statements if s.nodeType == "expression_statement"
             and s.parentId == "Enrollment.aspx"}
    assert texts == {"<%= Title %>", '<%# Eval("Id") %>'}  # never the `_= x ;` wrapper
    assert not any("Resources:Main" in s.text for s in rec.statements
                   if s.nodeType == "expression_statement")  # <%$ %> is not code


def test_a_vb_page_is_not_read_as_csharp(tmp_path) -> None:
    vb = _SERVER_CODE_PAGE.replace(b'Language="C#"', b'Language="VB"')
    rec = _parse_markup(tmp_path, "Enrollment.aspx", vb)
    assert rec.functions == []
    assert not any(s.nodeType == "if_statement" for s in rec.statements)


def test_page_links_scripts_and_script_files(tmp_path) -> None:
    (tmp_path / "Scripts").mkdir()
    (tmp_path / "Scripts" / "jquery.js").write_text("")
    rec = _parse_markup(tmp_path, "Enrollment.aspx", _SERVER_CODE_PAGE)
    nav = [(s.startLine, s.endpoint) for s in rec.statements if s.routeKind == "navigation"]
    assert nav == [(4, "/Admin.aspx")]
    calls = [(s.startLine, s.framework, s.method, s.endpoint)
             for s in rec.statements if s.semanticType == "api_call"]
    assert calls == [(7, "jquery", "POST", "/api/track")]
    assert "Scripts/jquery.js" in rec.importFiles  # <script src> in the repository → file link


# --- event handlers and user controls lead to their C# declaration -------------------------

_HOST = (b'<%@ Page Language="C#" CodeBehind="Orders.aspx.cs" Inherits="Shop.Orders" %>\n'
         b'<%@ Register TagPrefix="uc" TagName="DateField" Src="~/Controls/DateField.ascx" %>\n'
         b'<uc:DateField ID="Start" runat="server" />\n'
         b'<asp:Button ID="Save" runat="server" OnClick="Save_Click" />\n'
         b'<asp:Button ID="Mine" runat="server" OnClick="Own_Click" />\n'
         b'<asp:Button ID="Twice" runat="server" OnClick="Twice_Click" />\n'
         b'<asp:Button ID="Base" runat="server" OnClick="FromBase_Click" />\n'
         b'<script runat="server">\n'
         b'    void Own_Click(object s, EventArgs e) { }\n'
         b'</script>\n')
_HOST_CS = """namespace Shop
{
    public partial class Orders : BasePage
    {
        protected void Save_Click(object s, EventArgs e) { }
        protected void Twice_Click(object s, EventArgs e) { }
        protected void Twice_Click(object s) { }
    }
}
namespace Other { public class Orders { void FromBase_Click() { } } }
"""
_CONTROL = '<%@ Control Language="C#" CodeBehind="DateField.ascx.cs" Inherits="Shop.Controls.DateField" %>\n'
_CONTROL_CS = "namespace Shop.Controls\n{\n    public partial class DateField : TextBox { }\n}\n"


def _host(tmp_path: Path, *, host: bytes = _HOST, host_cs: str | None = _HOST_CS,
          control: str = _CONTROL, control_cs: str = _CONTROL_CS) -> dict[str, tuple]:
    (tmp_path / "Controls").mkdir()
    (tmp_path / "Controls" / "DateField.ascx").write_text(control)
    (tmp_path / "Controls" / "DateField.ascx.cs").write_text(control_cs)
    if host_cs is not None:
        (tmp_path / "Orders.aspx.cs").write_text(host_cs)
    (tmp_path / "Orders.aspx").write_bytes(host)
    ctx = ParseContext(path="Orders.aspx", abs_path=tmp_path / "Orders.aspx", source=host,
                       repo_root=tmp_path, capture_statements=True)
    rec = WebFormsParser().parse_file(ctx)
    return {s.handler: (s.handlerPath, s.handlerLine) for s in rec.statements if s.handler}


def test_event_handler_points_at_its_code_behind_method(tmp_path) -> None:
    got = _host(tmp_path)
    assert got["Save_Click"] == ("Orders.aspx.cs", 5)
    assert got["Own_Click"] == (None, 9)  # in the page itself: a line, no other file


def test_event_handler_with_two_or_no_declarations_gets_no_location(tmp_path) -> None:
    got = _host(tmp_path)
    assert got["Twice_Click"] == (None, None)  # overloads: which one is not known
    assert got["FromBase_Click"] == (None, None)  # only in another namespace's class


def test_user_control_points_at_its_class(tmp_path) -> None:
    assert _host(tmp_path)["DateField"] == ("Controls/DateField.ascx.cs", 3)


def test_user_control_class_declared_twice_keeps_the_ascx_link(tmp_path) -> None:
    unqualified = _CONTROL.replace("Shop.Controls.DateField", "DateField")
    twice = _CONTROL_CS + "namespace Old { public class DateField { } }\n"
    got = _host(tmp_path, control=unqualified, control_cs=twice)
    assert got["DateField"] == ("Controls/DateField.ascx", None)  # as before: no class link


def test_a_qualified_inherits_picks_its_namespace(tmp_path) -> None:
    twice = _CONTROL_CS + "namespace Old { public class DateField { } }\n"
    assert _host(tmp_path, control_cs=twice)["DateField"] == ("Controls/DateField.ascx.cs", 3)


def test_vb_code_behind_gives_no_location(tmp_path) -> None:
    vb = _HOST.replace(b'CodeBehind="Orders.aspx.cs"', b'CodeBehind="Orders.aspx.vb"')
    (tmp_path / "Orders.aspx.vb").write_text("Partial Class Orders\nEnd Class\n")
    got = _host(tmp_path, host=vb.replace(b'Language="C#"', b'Language="VB"'), host_cs=None)
    assert got["Save_Click"] == (None, None)


# --- data sources: SqlDataSource commands and ObjectDataSource methods ---------------------

_DS_PAGE = """<%@ Page Language="C#" %>
<asp:SqlDataSource ID="OrdersDS" runat="server"
    SelectCommand="SELECT Id FROM Orders"
    UpdateCommand="UPDATE Orders SET Total = @Total" />
<asp:SqlDataSource ID="ProcDS" runat="server" SelectCommandType="StoredProcedure"
    SelectCommand="usp_GetOrders" DeleteCommand="<%$ AppSettings:DeleteSql %>" />
<asp:ObjectDataSource ID="RepoDS" runat="server" TypeName="Shop.OrderRepo"
    SelectMethod="GetAll" UpdateMethod="Missing" />
"""
_REPO_CS = "namespace Shop\n{\n    public class OrderRepo\n    {\n        public object GetAll() { return null; }\n    }\n}\n"


def _data_sources(tmp_path: Path, files: dict[str, str]) -> list[tuple]:
    from breezeai_cog import iter_file_records

    for rel, text in files.items():
        (tmp_path / rel).write_text(text)
    [page] = [r for r in iter_file_records(tmp_path, capture_templates=True)
              if r.path == "Orders.aspx"]
    return [(s.startLine, s.semanticType, s.name, s.handler, s.handlerPath, s.handlerLine)
            for s in page.statements if s.semanticType == "query_statement" or s.handler]


def test_sql_data_source_commands_are_queries(tmp_path) -> None:
    got = _data_sources(tmp_path, {"Orders.aspx": _DS_PAGE, "OrderRepo.cs": _REPO_CS})
    queries = [(line, name) for line, kind, name, *_ in got if kind == "query_statement"]
    # one per command, named by the control's ID; a stored procedure counts; <%$ %> is not a query
    assert queries == [(3, "OrdersDS"), (4, "OrdersDS"), (6, "ProcDS")]


def test_object_data_source_method_points_at_its_declaration(tmp_path) -> None:
    got = _data_sources(tmp_path, {"Orders.aspx": _DS_PAGE, "OrderRepo.cs": _REPO_CS})
    methods = {handler: (path, line) for _, _, name, handler, path, line in got
               if name and name.endswith("Method")}
    assert methods == {"GetAll": ("OrderRepo.cs", 5), "Missing": (None, None)}


def test_object_data_source_with_two_classes_of_that_name_gets_no_location(tmp_path) -> None:
    other = _REPO_CS.replace("namespace Shop", "namespace Old")
    got = _data_sources(tmp_path, {"Orders.aspx": _DS_PAGE, "OrderRepo.cs": _REPO_CS,
                                   "OldRepo.cs": other})
    assert [(h, p, ln) for *_, h, p, ln in got if h == "GetAll"] == [("GetAll", None, None)]


# --- unbalanced markup: tags split across <% if %> / <% else %> -----------------------------

@pytest.mark.parametrize(("body", "expected"), [
    # one opening tag per branch, one shared closing tag
    ('<% if (w) { %><div class="a"><% } else { %><div class="b"><% } %>\n'
     '<asp:TextBox ID="N" runat="server" />\n<a href="/one.aspx">one</a>\n</div>\n',
     [(2, "asp:TextBox"), (3, "/one.aspx")]),
    # a row closed in one branch, a table closed in the other, inside a loop
    ('<table>\n<% foreach (var r in Rows) { %>\n<tr><td><asp:Label ID="L" runat="server" /></td>\n'
     '<% if (r.Last) { %></tr></table><% } else { %></tr><% } %>\n<% } %>\n'
     '<form action="/send" method="post"><asp:TextBox ID="T" runat="server" /></form>\n',
     [(3, "asp:Label"), (6, "/send"), (6, "asp:TextBox")]),
    # server controls opened in both branches and closed once (plus stray closing tags)
    ('<% if (x) { %><asp:Panel ID="P1" runat="server"><% } else { %>'
     '<asp:Panel ID="P2" runat="server"><% } %>\n<asp:TextBox ID="In" runat="server" />\n'
     '</asp:Panel>\n</asp:Panel></div></td>\n<a href="/end.aspx">end</a>\n',
     [(1, "asp:Panel"), (1, "asp:Panel"), (2, "asp:TextBox"), (5, "/end.aspx")]),
])
def test_unbalanced_markup_still_records_every_element(tmp_path, body, expected) -> None:
    page = b'<%@ Page Language="C#" %>\n' + body.encode()
    rec = _parse_markup(tmp_path, "Page.aspx", page)
    got = sorted((s.startLine - 1, s.name if s.nodeType == "element" else s.endpoint)
                 for s in rec.statements if s.nodeType == "element" or s.routeKind
                 or s.semanticType == "api_call")
    assert got == expected


def test_form_record_keeps_the_server_code_in_its_text(tmp_path) -> None:
    page = (b'<%@ Page Language="C#" %>\n'
            b'<form action="<%= ResolveUrl("~/Orders/Save") %>" method="post"></form>\n')
    rec = _parse_repo(WebFormsParser(), tmp_path, "Orders.aspx", {"Orders.aspx": page})
    form = next(s for s in rec.statements if s.semanticType == "api_call")
    assert (form.startLine, form.method, form.endpoint) == (2, "POST", None)
    assert form.text == '<form action="<%= ResolveUrl("~/Orders/Save") %>" method="post">'
