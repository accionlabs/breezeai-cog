from __future__ import annotations

from pathlib import Path

from breezeai_cog import iter_file_records
from breezeai_cog.parsers.contract import is_entry_point


def _routes(root: Path):
    return [
        statement
        for record in iter_file_records(root, capture_templates=True)
        for statement in record.statements
        if is_entry_point(statement)
    ]


def test_register_routes_is_wiring_and_actions_are_bindings(tmp_path: Path) -> None:
    module = tmp_path / "DesktopModules" / "Nested" / "Demo"
    module.mkdir(parents=True)
    (module / "OrdersController.cs").write_text(
        """
        using DotNetNuke.Web.Api;
        using System.Web.Http;
        public class RouteMapper : IServiceRouteMapper {
            public void RegisterRoutes(IMapRoute map) {
                map.MapHttpRoute("Nested/Demo", "default", "{controller}/{action}");
            }
        }
        public class OrdersController : DnnApiController {
            [HttpPost] public object Create() => null;
        }
        """,
        encoding="utf-8",
    )
    routes = _routes(tmp_path)
    assert [(route.endpoint, route.method, route.handler) for route in routes] == [
        ("/DesktopModules/Nested/Demo/API/Orders/Create", "POST", "Create")
    ]


def test_non_public_and_non_action_methods_are_not_bindings(tmp_path: Path) -> None:
    module = tmp_path / "DesktopModules" / "Demo"
    module.mkdir(parents=True)
    (module / "OrdersController.cs").write_text(
        """
        using DotNetNuke.Web.Api;
        public class OrdersController : DnnApiController {
            private object Hidden() => null;
            [NonAction] public object Helper() => null;
            public object Visible() => null;
        }
        """,
        encoding="utf-8",
    )
    assert [route.handler for route in _routes(tmp_path)] == ["Visible"]


def test_dynamic_module_prefix_is_left_unresolved(tmp_path: Path) -> None:
    module = tmp_path / "DesktopModules" / "Demo"
    module.mkdir(parents=True)
    (module / "OrdersController.cs").write_text(
        """
        using DotNetNuke.Web.Api;
        public class RouteMapper : IServiceRouteMapper {
            public void RegisterRoutes(IMapRoute map, string prefix) {
                map.MapHttpRoute(prefix, "default", "{controller}/{action}");
            }
        }
        public class OrdersController : DnnApiController {
            public object Visible() => null;
        }
        """,
        encoding="utf-8",
    )
    assert _routes(tmp_path) == []


def test_module_authorization_is_carried_to_the_route(tmp_path: Path) -> None:
    module = tmp_path / "DesktopModules" / "Demo"
    module.mkdir(parents=True)
    (module / "OrdersController.cs").write_text(
        """
        using DotNetNuke.Web.Api;
        public class OrdersController : DnnApiController {
            [DnnModuleAuthorize] public object Visible() => null;
        }
        """,
        encoding="utf-8",
    )
    routes = _routes(tmp_path)
    assert routes[0].guards == ["DnnModuleAuthorize"]
    assert routes[0].authRequired is True
