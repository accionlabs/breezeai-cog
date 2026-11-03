from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records
from breezeai_cog.parsers.contract import is_entry_point
from breezeai_cog.schemas import FileRecord


CONTROLLER = b"""
using DotNetNuke.Web.Api;
namespace Demo {
  public class RouteMapper : IServiceRouteMapper {
    public void RegisterRoutes(IMapRoute map) {
      map.MapHttpRoute("Demo", "default", "{controller}/{action}");
    }
  }
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
    assert routes[1].guards == ["AllowAnonymous", "ValidateAntiForgeryToken"]
    assert routes[1].authRequired is False


def test_dnn_manifest_mounts_portal_control(tmp_path: Path) -> None:
    module = tmp_path / "DesktopModules" / "Demo"
    module.mkdir(parents=True)
    (module / "Demo.dnn").write_text(
        "<dotnetnuke><foldername>Demo</foldername>"
        "<moduleControl><controlKey>Orders</controlKey>"
        "<controlSrc>DesktopModules/Demo/Orders.ascx</controlSrc></moduleControl>"
        "</dotnetnuke>",
        encoding="utf-8",
    )
    (module / "Orders.ascx").write_text("<%@ Control %>", encoding="utf-8")
    (module / "Orders.ascx.cs").write_bytes(CONTROL)
    records = _records(tmp_path)
    mounts = [s for r in records for s in r.statements if s.routeKind == "mount"]
    assert [(s.endpoint, s.handler) for s in mounts] == [
        ("/DesktopModules/Demo/Orders", "OrdersView")
    ]
    manifest = next(r for r in records if r.path.endswith("Demo.dnn"))
    assert manifest.importFiles == ["DesktopModules/Demo/Orders.ascx"]


def test_dnn_manifest_preserves_empty_default_control_key(tmp_path: Path) -> None:
    module = tmp_path / "DesktopModules" / "Nested" / "Demo"
    module.mkdir(parents=True)
    (module / "Demo.dnn").write_text(
        "<dotnetnuke><moduleName>DemoPackage</moduleName>"
        "<foldername>Nested/Demo</foldername><moduleControl>"
        "<controlKey /><controlSrc>DesktopModules/Nested/Demo/View.ascx</controlSrc>"
        "</moduleControl></dotnetnuke>",
        encoding="utf-8",
    )
    (module / "View.ascx").write_text("<%@ Control %>", encoding="utf-8")
    (module / "View.ascx.cs").write_bytes(CONTROL)
    records = _records(tmp_path)
    mounts = [s for r in records for s in r.statements if s.routeKind == "mount"]
    assert [(s.endpoint, s.handler) for s in mounts] == [
        ("/DesktopModules/Nested/Demo/", "OrdersView")
    ]


def test_dnn_manifest_keeps_multiple_module_controls_associated(tmp_path: Path) -> None:
    install = tmp_path / "Install"
    install.mkdir()
    (install / "Bundle.dnn").write_text(
        """
        <dotnetnuke type="Package" version="9.0">
          <packages>
            <package name="One" type="Module">
              <components><component type="Module"><desktopModule>
                <moduleName>OnePackage</moduleName><foldername>One</foldername>
                <moduleDefinition><moduleControls><moduleControl>
                  <controlKey /><controlSrc>DesktopModules/One/View.ascx</controlSrc>
                </moduleControl></moduleControls></moduleDefinition>
              </desktopModule></component></components>
            </package>
            <package name="Two" type="Module">
              <components><component type="Module"><desktopModule>
                <moduleName>TwoPackage</moduleName><foldername>Two</foldername>
                <moduleDefinition><moduleControls><moduleControl>
                  <controlKey>Edit</controlKey><controlSrc>DesktopModules/Two/Edit.ascx</controlSrc>
                </moduleControl></moduleControls></moduleDefinition>
              </desktopModule></component></components>
            </package>
          </packages>
        </dotnetnuke>
        """,
        encoding="utf-8",
    )
    for module, control, class_name in (("One", "View", "OneView"), ("Two", "Edit", "TwoEdit")):
        folder = tmp_path / "DesktopModules" / module
        folder.mkdir(parents=True)
        (folder / f"{control}.ascx").write_text("<%@ Control %>", encoding="utf-8")
        (folder / f"{control}.ascx.cs").write_text(
            f"using DotNetNuke.UI.Modules; public class {class_name} : PortalModuleBase {{ }}",
            encoding="utf-8",
        )
    records = _records(tmp_path)
    mounts = [s for r in records for s in r.statements if s.routeKind == "mount"]
    assert {(s.endpoint, s.handler) for s in mounts} == {
        ("/DesktopModules/One/", "OneView"),
        ("/DesktopModules/Two/Edit", "TwoEdit"),
    }
    manifest = next(r for r in records if r.path.endswith("Bundle.dnn"))
    assert manifest.importFiles == [
        "DesktopModules/One/View.ascx",
        "DesktopModules/Two/Edit.ascx",
    ]
    assert [(c["module"], c["controlKey"]) for c in manifest.metadata["moduleControls"]] == [
        ("One", ""),
        ("Two", "Edit"),
    ]


def test_dnn_non_controller_mention_is_not_a_route(tmp_path: Path) -> None:
    (tmp_path / "Mention.cs").write_bytes(
        b'class Mention { string Text = "DnnApiController PortalModuleBase"; }'
    )
    records = _records(tmp_path)
    assert not [s for r in records for s in r.statements if s.framework == "dnn"]
