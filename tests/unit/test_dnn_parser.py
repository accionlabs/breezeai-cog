from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records
from breezeai_cog.parsers.contract import is_entry_point
from breezeai_cog.schemas import FileRecord


CONTROLLER = b"""
using DotNetNuke.Web.Api;
namespace Demo {
  public class OrdersController : DnnApiController {
    [DnnAuthorize] public object List() { return null; }
    [AllowAnonymous] [ValidateAntiForgeryToken] public object Preview() { return null; }
  }
}
"""

CONTROL = b"""
using DotNetNuke.UI.Modules;
namespace Demo {
  public partial class OrdersView : PortalModuleBase { }
}
"""


def _records(root: Path) -> list[FileRecord]:
    return list(iter_file_records(root, capture_templates=True))


def test_dnn_api_actions_have_full_routes_and_metadata(tmp_path: Path) -> None:
    (tmp_path / "DesktopModules" / "Demo").mkdir(parents=True)
    (tmp_path / "DesktopModules" / "Demo" / "OrdersController.cs").write_bytes(CONTROLLER)
    records = _records(tmp_path)
    routes = [s for r in records for s in r.statements if is_entry_point(s)]
    assert [(r.endpoint, r.method) for r in routes] == [
        ("/DesktopModules/Demo/API/Orders/List", "GET"),
        ("/DesktopModules/Demo/API/Orders/Preview", "GET"),
    ]
    assert routes[0].guards == ["DnnAuthorize"]
    assert routes[0].authRequired is True
    assert routes[1].guards == ["ValidateAntiForgeryToken"]
    assert routes[1].authRequired is False


def test_dnn_manifest_mounts_portal_control(tmp_path: Path) -> None:
    module = tmp_path / "DesktopModules" / "Demo"
    module.mkdir(parents=True)
    (module / "Demo.dnn").write_text(
        "<dotnetnuke><foldername>Demo</foldername>"
        '<moduleControl controlKey="Orders" controlSrc="DesktopModules/Demo/Orders.ascx" />'
        "</dotnetnuke>",
        encoding="utf-8",
    )
    (module / "Orders.ascx").write_text("<%@ Control %>", encoding="utf-8")
    (module / "Orders.ascx.cs").write_bytes(CONTROL)
    records = _records(tmp_path)
    mounts = [
        s for r in records for s in r.statements
        if s.routeKind == "mount"
    ]
    assert [(s.endpoint, s.handler) for s in mounts] == [
        ("/DesktopModules/Demo/Orders", "OrdersView")
    ]
    manifest = next(r for r in records if r.path.endswith("Demo.dnn"))
    assert manifest.importFiles == ["DesktopModules/Demo/Orders.ascx"]
