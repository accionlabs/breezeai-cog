from __future__ import annotations

from pathlib import Path

import pytest

from breezeai_cog.config import Settings
from breezeai_cog.parsers.contract import is_entry_point
from breezeai_cog.services import AnalysisService


def _records(repo: Path, jobs: int, capture_templates: bool = False) -> list:
    settings = Settings(
        repo=repo,
        capture_templates=capture_templates,
        jobs=jobs,
    )
    return list(AnalysisService(settings).iter_file_records(repo))


@pytest.mark.parametrize("jobs", [1, 2])
def test_route_prefixes_resolved_across_files(tmp_path: Path, jobs: int) -> None:
    (tmp_path / "RouteMapper.cs").write_text(
        """
        using DotNetNuke.Web.Api;
        public class RouteMapper : IServiceRouteMapper {
            public void RegisterRoutes(IMapRoute map) {
                map.MapHttpRoute("Demo", "default", "{controller}/{action}");
            }
        }
        """,
        encoding="utf-8",
    )
    folder = tmp_path / "DesktopModules" / "Demo"
    folder.mkdir(parents=True)
    (folder / "OrdersController.cs").write_text(
        """
        using DotNetNuke.Web.Api;
        using System.Web.Http;
        public class OrdersController : DnnApiController {
            [HttpGet] public object List() => null;
        }
        """,
        encoding="utf-8",
    )
    records = _records(tmp_path, jobs)
    routes = [s for r in records for s in r.statements if is_entry_point(s)]
    assert [(r.endpoint, r.method, r.handler) for r in routes] == [
        ("/DesktopModules/Demo/API/Orders/List", "GET", "List")
    ]


@pytest.mark.parametrize("jobs", [1, 2])
def test_manifest_control_emits_no_entry_point_across_files(tmp_path: Path, jobs: int) -> None:
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
    (module / "Orders.ascx.cs").write_text(
        "using DotNetNuke.UI.Modules; public class OrdersView : PortalModuleBase { }",
        encoding="utf-8",
    )
    records = _records(tmp_path, jobs, capture_templates=True)
    mounts = [s for r in records for s in r.statements if s.routeKind == "mount"]
    assert mounts == []
