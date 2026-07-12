"""ASP.NET Web Forms parser (C#): page/control route detection (convention-based,
file-parented), capture-gating, master/base-class skipping, and parser selection."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.csharp.parser import CSharpParser
from breezeai_cog.parsers.csharp_aspnet.parser import AspNetCoreParser
from breezeai_cog.parsers.csharp_webforms.parser import WebFormsParser
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
