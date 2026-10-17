"""AspNetCoreParser — a C# framework parser. Selected (one parser per file) over
CSharpParser when ``claims`` finds an ASP.NET namespace; reuses ``CSharpParser.extract``
(single parse), then detects controller routes (off the captured attributes) and
minimal-API ``app.MapGet(…)`` endpoints (AST walk). Covers ASP.NET Core MVC/Web API,
minimal APIs, and classic ASP.NET (System.Web)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..method_lines import locate_method
from ..base import ParseContext
from ..emits import apply
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from ..aspnet_common import (
    detect_controller_routes,
    detect_minimal_api_routes,
    detect_route_registrations,
)

_MARKERS = (b"Microsoft.AspNetCore", b"System.Web.Mvc", b"System.Web.Http")


class AspNetCoreParser(CSharpParser):
    name = "csharp-aspnet"
    priority = 10
    frameworks = ["aspnet", "aspnetcore"]

    def claims(self, path: str, source: bytes) -> bool:
        return any(m in source for m in _MARKERS)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        if ctx.capture_statements:  # routes are statements — gated by statement capture
            routes = detect_controller_routes(record, root, ctx.resolution_index)
            routes += detect_minimal_api_routes(
                root, ctx.source, ctx.path,
                invocation_type="invocation_expression", member_type="member_access_expression",
            )
            registrations, wiring = detect_route_registrations(
                root, ctx.source, ctx.path,
                invocation_type="invocation_expression", member_type="member_access_expression",
            )
            # A template's fixed default `Controller.Action` → that action method (another file).
            for e in registrations:
                ctrl, _, action = (e.fields.get("handler") or "").partition(".")
                site = locate_method(ctx.resolution_index, "csharp", ctrl + "Controller", action) \
                    if ctrl and action else None
                if site is not None:
                    e.fields.update(handler=action, handlerLine=site[1],
                                    handlerPath=site[0] if site[0] != ctx.path else None)
            routes += registrations
            if routes or wiring:  # a wiring-only RouteConfig is still ASP.NET code
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "aspnet"
        return record
