"""AspNetCoreParser — a C# framework parser. Selected (one parser per file) over
CSharpParser when ``claims`` finds an ASP.NET namespace; reuses ``CSharpParser.extract``
(single parse), then detects controller routes (off the captured attributes) and
minimal-API ``app.MapGet(…)`` endpoints (AST walk). Covers ASP.NET Core MVC/Web API,
minimal APIs, and classic ASP.NET (System.Web). A file with no ASP.NET namespace (implicit
usings) keeps its own parser; :mod:`.web_projects` adds its routes."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..handler_sites import handler_location, locate_method
from ..base import ParseContext
from ..emits import Emit, apply
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from ..aspnet_common import (
    detect_controller_routes,
    detect_minimal_api_routes,
    detect_route_registrations,
)
from .group_extensions import group_extension_prefix

_MARKERS = (b"Microsoft.AspNetCore", b"System.Web.Mvc", b"System.Web.Http")


def owned_by_aspnet(source: bytes) -> bool:
    """Whether :class:`AspNetCoreParser` claims a file with this source. Shared with the
    web-project detector (:mod:`.web_projects`) so the two never both emit for one file."""
    return any(m in source for m in _MARKERS)


def detect_aspnet_routes(record: FileRecord, root: Node, ctx: ParseContext) -> tuple[list[Emit], bool]:
    """Controller, minimal-API and convention-registration routes of one C# file, and whether
    any registration was seen (a wiring-only ``RouteConfig`` is still ASP.NET code)."""
    index = ctx.resolution_index
    routes = detect_controller_routes(record, root, index, ctx.source)
    routes += detect_minimal_api_routes(
        root, ctx.source, ctx.path,
        invocation_type="invocation_expression", member_type="member_access_expression",
        extension_prefix=lambda method: group_extension_prefix(index, method),
    )
    registrations, wiring = detect_route_registrations(
        root, ctx.source, ctx.path,
        invocation_type="invocation_expression", member_type="member_access_expression",
    )
    # A template's fixed default `Controller.Action` → that action method (another file).
    for e in registrations:
        ctrl, _, action = (e.fields.get("handler") or "").partition(".")
        site = locate_method(index, "csharp", ctrl + "Controller", action) \
            if ctrl and action else None
        if site is not None:
            line, path = handler_location(site, ctx.path)
            e.fields.update(handler=action, handlerLine=line, handlerPath=path)
    return routes + registrations, wiring


class AspNetCoreParser(CSharpParser):
    name = "csharp-aspnet"
    priority = 10
    frameworks = ["aspnet", "aspnetcore"]

    def claims(self, path: str, source: bytes) -> bool:
        return owned_by_aspnet(source)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        # Routes are statements — gated by statement capture. A file this parser does not own
        # (parsed directly) gets its routes from the web-project detector, never from both.
        if ctx.capture_statements and owned_by_aspnet(ctx.source):
            routes, wiring = detect_aspnet_routes(record, root, ctx)
            if routes or wiring:  # a wiring-only RouteConfig is still ASP.NET code
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "aspnet"
        return record
