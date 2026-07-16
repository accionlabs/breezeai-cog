"""ASP.NET Web Forms parser (C#): page/control route detection (convention-based,
file-parented), capture-gating, master/base-class skipping, and parser selection."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.csharp_aspnet.parser import AspNetCoreParser
from breezeai_cog.parsers.csharp_webforms.parser import WebFormsParser
from breezeai_cog.parsers.csharp_webforms.routes import (
    detect_master_layout,
    detect_webforms_pages,
)
from breezeai_cog.schemas import FileRecord

PAGE = b'''
using System;
using System.Web.UI;
namespace Acme {
  public partial class Enrollment : Page {
    protected void Page_Load(object sender, EventArgs e) { }
  }
}
'''

CONTROL = b'''
using System;
using System.Web.UI;
namespace Acme {
  public partial class ButtonNavigation : UserControl {
    protected void Page_Load(object sender, EventArgs e) { }
  }
}
'''

MVC = b'''
using Microsoft.AspNetCore.Mvc;
namespace Acme {
  [ApiController]
  [Route("api/orders")]
  public class OrdersController : ControllerBase {
    [HttpGet] public object Get() { return null; }
  }
}
'''


def _parse(parser, src, name, *, capture=True) -> FileRecord:
    ctx = ParseContext(path=name, abs_path=None, source=src, repo_root=None, capture_statements=capture)
    return parser.parse_file(ctx)


def _parse_repo(parser, root: Path, target: str, files: dict[str, bytes],
                *, capture=True) -> FileRecord:
    """Write ``files`` under ``root`` and parse ``target`` with on-disk context (so the
    markup pass can read the sibling markup + verify control code-behind targets)."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    ctx = ParseContext(
        path=target, abs_path=root / target, source=(root / target).read_bytes(),
        repo_root=root, capture_statements=capture,
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
    assert r.nodeType == "synthetic"          # normalized synthetic marker (no backing AST node)
    assert r.framework == "aspnet-webforms"
    assert r.method == "GET"
    assert r.endpoint == "/CMS/Enrollment.aspx"
    assert r.handler == "Enrollment"          # code-behind class = markup stem
    assert r.parentId == "CMS/Enrollment.aspx.cs"  # file-parented (like React)
    assert rec.framework == "aspnet-webforms"


def test_control_mounts() -> None:
    rec = _parse(WebFormsParser(), CONTROL, "CMS/Controls/ButtonNavigation.ascx.cs")
    routes = [s for s in rec.statements if s.semanticType == "route"]
    assert len(routes) == 1
    assert routes[0].routeKind == "mount"
    assert routes[0].endpoint == "/CMS/Controls/ButtonNavigation.ascx"
    assert routes[0].handler == "ButtonNavigation"


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
    rec = _parse_repo(WebFormsParser(), tmp_path, "CMS/Page.aspx.cs", {
        "web.config": b"<configuration/>",                       # app root = repo root
        "CMS/Page.aspx": b'<%@ Register TagName="Nav" Src="~/CMS/Controls/Nav.ascx" %>\n<html/>',
        "CMS/Page.aspx.cs": PAGE,
        "CMS/Controls/Nav.ascx.cs": CTRL_CB,                     # target exists
    })
    assert "CMS/Controls/Nav.ascx.cs" in rec.importFiles
    assert rec.framework == "aspnet-webforms"


def test_mount_loadcontrol_literal(tmp_path: Path) -> None:
    # `LoadControl("~/…")` in the code-behind resolves the same way.
    cb = b'using System.Web.UI;\nnamespace Acme { public partial class P : Page {' \
         b' void L(){ LoadControl("~/Controls/Cart.ascx"); } } }\n'
    rec = _parse_repo(WebFormsParser(), tmp_path, "Shop/P.aspx.cs", {
        "web.config": b"<configuration/>",
        "Shop/P.aspx.cs": cb,
        "Controls/Cart.ascx.cs": CTRL_CB,
    })
    assert "Controls/Cart.ascx.cs" in rec.importFiles


def test_mount_dynamic_loadcontrol_unresolved(tmp_path: Path) -> None:
    # A data-driven control name (no string literal) is honest-null — no edge.
    cb = b'using System.Web.UI;\nnamespace Acme { public partial class P : Page {' \
         b' void L(string n){ LoadControl(n); } } }\n'
    rec = _parse_repo(WebFormsParser(), tmp_path, "P.aspx.cs", {
        "web.config": b"<configuration/>", "P.aspx.cs": cb,
    })
    assert rec.importFiles == []


def test_mount_missing_codebehind_skipped(tmp_path: Path) -> None:
    # Registered control whose .ascx.cs does not exist (inline-code control) → no dangling edge.
    rec = _parse_repo(WebFormsParser(), tmp_path, "Page.aspx.cs", {
        "web.config": b"<configuration/>",
        "Page.aspx": b'<%@ Register Src="~/Controls/Inline.ascx" %>',
        "Page.aspx.cs": PAGE,   # Controls/Inline.ascx.cs intentionally absent
    })
    assert rec.importFiles == []


def test_mount_app_root_in_subdir(tmp_path: Path) -> None:
    # `~/` resolves against the app root (nearest web.config), not the repo root.
    rec = _parse_repo(WebFormsParser(), tmp_path, "App/CMS/Page.aspx.cs", {
        "App/web.config": b"<configuration/>",                   # app root = App/
        "App/CMS/Page.aspx": b'<%@ Register Src="~/Controls/Nav.ascx" %>',
        "App/CMS/Page.aspx.cs": PAGE,
        "App/Controls/Nav.ascx.cs": CTRL_CB,                     # ~/ → App/Controls/…
    })
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
        return build_csharp_index(root, files).page_routes


ROUTECFG = b'''
using System.Web.Routing;
namespace Acme {
  public static class RouteConfig {
    public static void Register(RouteCollection routes) {
      routes.MapPageRoute("enroll", "enroll/{id}", "~/CMS/Enrollment.aspx");
    }
  }
}
'''


def test_mappageroute_index_extracts_literal() -> None:
    pr = _page_routes(ROUTECFG)
    assert pr == {"CMS/Enrollment.aspx": ["enroll/{id}"]}


def test_mappageroute_ast_only_ignores_comment_and_string() -> None:
    # A MapPageRoute in a comment or string must NOT be picked up (AST extraction, not regex).
    src = b'''
namespace X { class C {
  // routes.MapPageRoute("ghost", "ghost/{id}", "~/Ghost.aspx");
  string s = "routes.MapPageRoute(\\"s\\",\\"s\\",\\"~/S.aspx\\")";
} }
'''
    assert _page_routes(src) == {}


def test_mappageroute_dynamic_arg_unresolved() -> None:
    # Non-literal physical/url arg → honest-null, no mapping.
    src = b'''
namespace X { class C { void R(System.Web.Routing.RouteCollection routes, string phys) {
  routes.MapPageRoute("n", "url/{id}", phys);
} } }
'''
    assert _page_routes(src) == {}


def test_page_route_uses_friendly_url() -> None:
    rec = _parse(WebFormsParser(), PAGE, "CMS/Enrollment.aspx.cs")
    routes = detect_webforms_pages(rec, "CMS/Enrollment.aspx.cs",
                                   {"CMS/Enrollment.aspx": ["enroll/{id}"]})
    assert len(routes) == 1
    assert routes[0].endpoint == "/enroll/{id}"          # friendly, not /CMS/Enrollment.aspx
    assert routes[0].routeKind == "page"


def test_page_route_multiple_friendly_urls() -> None:
    routes = detect_webforms_pages(_parse(WebFormsParser(), PAGE, "P.aspx.cs"), "P.aspx.cs",
                                   {"P.aspx": ["a/{id}", "b/{id}"]})
    assert sorted(r.endpoint for r in routes) == ["/a/{id}", "/b/{id}"]
    assert len({r.id for r in routes}) == 2              # distinct ids


def test_page_route_physical_fallback_without_mapping() -> None:
    routes = detect_webforms_pages(_parse(WebFormsParser(), PAGE, "CMS/Enrollment.aspx.cs"),
                                   "CMS/Enrollment.aspx.cs", {"Other.aspx": ["x"]})
    assert routes[0].endpoint == "/CMS/Enrollment.aspx"  # no match → physical, as before


def test_mount_not_friendly_routed() -> None:
    # A control (routeKind=mount) never takes a MapPageRoute URL, even if one keys its path.
    routes = detect_webforms_pages(_parse(WebFormsParser(), CONTROL, "Ctrl.ascx.cs"),
                                   "Ctrl.ascx.cs", {"Ctrl.ascx": ["should/not/apply"]})
    assert routes[0].endpoint == "/Ctrl.ascx"
    assert routes[0].routeKind == "mount"


MASTER_CB = b"using System.Web.UI;\nnamespace Acme { public partial class Site : MasterPage {} }\n"


def test_master_layout_page(tmp_path: Path) -> None:
    # `<%@ Page MasterPageFile %>` → a routeKind=layout statement alongside the page route.
    rec = _parse_repo(WebFormsParser(), tmp_path, "CMS/Enrollment.aspx.cs", {
        "web.config": b"<configuration/>",
        "CMS/Enrollment.aspx": b'<%@ Page MasterPageFile="~/Site.master" %>\n<html/>',
        "CMS/Enrollment.aspx.cs": PAGE,
        "Site.master": b'<%@ Master %>',                     # target must exist on disk
        "Site.master.cs": MASTER_CB,
    })
    layouts = [s for s in rec.statements if s.routeKind == "layout"]
    assert len(layouts) == 1
    assert layouts[0].endpoint == "/Site.master"
    assert layouts[0].semanticType == "route"
    assert layouts[0].nodeType == "synthetic"
    assert layouts[0].method is None                        # layout is composition, not a verb
    # page route still present and distinct
    assert {s.routeKind for s in rec.statements if s.semanticType == "route"} == {"page", "layout"}


def test_master_layout_nesting(tmp_path: Path) -> None:
    # A .master can itself declare a parent master (`<%@ Master MasterPageFile %>`).
    rec = _parse_repo(WebFormsParser(), tmp_path, "Site.master.cs", {
        "web.config": b"<configuration/>",
        "Site.master": b'<%@ Master MasterPageFile="~/Root.master" %>',
        "Site.master.cs": MASTER_CB,
        "Root.master": b'<%@ Master %>',
        "Root.master.cs": MASTER_CB,
    })
    layouts = [s for s in rec.statements if s.routeKind == "layout"]
    assert len(layouts) == 1 and layouts[0].endpoint == "/Root.master"


def test_master_missing_file_skipped(tmp_path: Path) -> None:
    # Declared master not present in the repo → honest-null, no layout statement.
    rec = _parse_repo(WebFormsParser(), tmp_path, "P.aspx.cs", {
        "web.config": b"<configuration/>",
        "P.aspx": b'<%@ Page MasterPageFile="~/Ghost.master" %>',
        "P.aspx.cs": PAGE,
    })
    assert [s for s in rec.statements if s.routeKind == "layout"] == []


def test_master_directive_only_not_comment(tmp_path: Path) -> None:
    # MasterPageFile in an HTML comment is not a directive → not captured.
    rec = _parse_repo(WebFormsParser(), tmp_path, "P.aspx.cs", {
        "web.config": b"<configuration/>",
        "P.aspx": b'<!-- MasterPageFile="~/Site.master" -->\n<html/>',
        "P.aspx.cs": PAGE,
        "Site.master": b'x', "Site.master.cs": MASTER_CB,
    })
    assert [s for s in rec.statements if s.routeKind == "layout"] == []


def test_master_layout_no_master_no_statement() -> None:
    assert detect_master_layout(_parse(WebFormsParser(), PAGE, "P.aspx.cs"), "P.aspx.cs", None) == []


def _nav_page(body: bytes) -> bytes:
    return b'''
using System; using System.Web.UI;
namespace Acme { public partial class Nav1 : Page {
  protected void Submit(object s, EventArgs e) { ''' + body + b''' }
} }
'''


def _navs(rec: FileRecord) -> list:
    return [s for s in rec.statements if s.routeKind == "navigation"]


def test_navigation_redirect_real_node() -> None:
    rec = _parse(WebFormsParser(), _nav_page(b'Response.Redirect("~/Login.aspx?ReturnUrl=x");'),
                 "CMS/Nav1.aspx.cs")
    navs = _navs(rec)
    assert len(navs) == 1
    assert navs[0].endpoint == "/Login.aspx"              # query stripped
    assert navs[0].nodeType == "invocation_expression"    # real call, not synthetic
    assert navs[0].semanticType == "route"


def test_navigation_server_transfer() -> None:
    rec = _parse(WebFormsParser(), _nav_page(b'Server.Transfer("~/Confirm.aspx");'), "P.aspx.cs")
    assert [s.endpoint for s in _navs(rec)] == ["/Confirm.aspx"]


def test_navigation_external_and_friendly_skipped() -> None:
    rec = _parse(WebFormsParser(),
                 _nav_page(b'Response.Redirect("https://x.com/a"); Response.Redirect("~/enroll/5");'),
                 "P.aspx.cs")
    assert _navs(rec) == []                                # external + friendly-URL (no .aspx) → none


def test_navigation_dynamic_and_wrong_receiver_skipped() -> None:
    rec = _parse(WebFormsParser(),
                 _nav_page(b'Response.Redirect(url); helper.Transfer("~/X.aspx");'), "P.aspx.cs")
    assert _navs(rec) == []                                # non-literal + non-Response/Server receiver


def test_navigation_markup_attrs(tmp_path: Path) -> None:
    rec = _parse_repo(WebFormsParser(), tmp_path, "CMS/Page.aspx.cs", {
        "web.config": b"<configuration/>",
        "CMS/Page.aspx": b'<asp:HyperLink NavigateUrl="~/Help.aspx"/>'
                         b'<asp:Button PostBackUrl="~/Submit.aspx"/>',
        "CMS/Page.aspx.cs": PAGE,
    })
    navs = _navs(rec)
    assert sorted(s.endpoint for s in navs) == ["/Help.aspx", "/Submit.aspx"]
    assert all(s.nodeType == "synthetic" for s in navs)   # markup attrs → synthetic


def test_mount_case_insensitive(tmp_path: Path) -> None:
    # Web Forms path refs are routinely mis-cased vs disk (case-insensitive Windows origin).
    # `~/Controls/NAV.ascx` must resolve to the real `Nav.ascx.cs` (so the IMPORTS join lands).
    rec = _parse_repo(WebFormsParser(), tmp_path, "Page.aspx.cs", {
        "web.config": b"<configuration/>",
        "Page.aspx": b'<%@ Register Src="~/Controls/NAV.ascx" %>',   # uppercase in the ref
        "Page.aspx.cs": PAGE,
        "Controls/Nav.ascx.cs": CTRL_CB,                            # real casing on disk
    })
    assert "Controls/Nav.ascx.cs" in rec.importFiles                # real casing, not "NAV"


def test_master_case_insensitive(tmp_path: Path) -> None:
    # `~/Root.Master` (capital M in the ref) → the real `Root.master` (SplendidCRM's exact case bug).
    rec = _parse_repo(WebFormsParser(), tmp_path, "P.aspx.cs", {
        "web.config": b"<configuration/>",
        "P.aspx": b'<%@ Page MasterPageFile="~/Root.Master" %>',
        "P.aspx.cs": PAGE,
        "Root.master": b'<%@ Master %>', "Root.master.cs": MASTER_CB,
    })
    layouts = [s for s in rec.statements if s.routeKind == "layout"]
    assert len(layouts) == 1 and layouts[0].endpoint == "/Root.master"   # real casing


def test_mount_case_collision_honest_null(tmp_path: Path) -> None:
    # Two controls differing only in case → the name is ambiguous → no mount (honest-null,
    # never an arbitrary pick). Only creatable on a case-sensitive filesystem.
    (tmp_path / "Controls").mkdir()
    (tmp_path / "Controls" / "Nav.ascx.cs").write_bytes(CTRL_CB)
    (tmp_path / "Controls" / "NAV.ascx.cs").write_bytes(CTRL_CB)
    if len(list((tmp_path / "Controls").iterdir())) < 2:
        import pytest
        pytest.skip("case-insensitive filesystem — collision not reproducible")
    rec = _parse_repo(WebFormsParser(), tmp_path, "Page.aspx.cs", {
        "web.config": b"<configuration/>",
        "Page.aspx": b'<%@ Register Src="~/Controls/nav.ascx" %>',
        "Page.aspx.cs": PAGE,
    })
    assert [i for i in rec.importFiles if i.lower().endswith(".ascx.cs")] == []


def test_output_validates() -> None:
    for src, name in [(PAGE, "CMS/Enrollment.aspx.cs"), (CONTROL, "CMS/Ctrl.ascx.cs")]:
        rec = _parse(WebFormsParser(), src, name)
        errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                      .iter_errors(json.loads(to_line(rec))))
        assert not errors, errors


def test_selection() -> None:
    registry.clear()
    for p in (CSharpParser(), AspNetCoreParser(), WebFormsParser()):
        registry.register(p)
    assert registry.select("CMS/Enrollment.aspx.cs", PAGE).name == "csharp-webforms"
    assert registry.select("CMS/Ctrl.ascx.cs", CONTROL).name == "csharp-webforms"
    assert registry.select("Orders.cs", MVC).name == "csharp-aspnet"   # disjoint claims
    assert registry.select("plain.cs", b"class C {}").name == "csharp"
    registry.clear()
