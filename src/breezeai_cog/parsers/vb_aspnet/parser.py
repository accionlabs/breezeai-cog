"""VbAspNetParser — a VB.NET framework parser. Mirrors the C# ASP.NET parser: reuses
``VbParser.extract`` (single parse), then the **language-agnostic** controller-route
detector (:func:`detect_controller_routes` reads the ``FileRecord``, so it is shared
verbatim with C#) plus a VB-node minimal-API walk."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..aspnet_common import (
    detect_controller_routes,
    detect_minimal_api_routes,
    detect_route_registrations,
)
from ..treesitter import parse_source
from ..vb.parser import VbParser

_MARKERS = (b"Microsoft.AspNetCore", b"System.Web.Mvc", b"System.Web.Http")


class VbAspNetParser(VbParser):
    name = "vb-aspnet"
    priority = 10
    frameworks = ["aspnet", "aspnetcore"]

    def claims(self, path: str, source: bytes) -> bool:
        return any(m in source for m in _MARKERS)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("vb", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited VB extraction (one parse)
        if ctx.capture_statements:  # routes are statements — gated by statement capture
            routes = detect_controller_routes(record, root, ctx.resolution_index)
            routes += detect_minimal_api_routes(
                root, ctx.source, ctx.path,
                invocation_type="invocation", member_type="member_access",
            )
            registrations, wiring = detect_route_registrations(
                root, ctx.source, ctx.path,
                invocation_type="invocation", member_type="member_access",
            )
            routes += registrations
            if routes or wiring:  # a wiring-only RouteConfig is still ASP.NET code
                apply(record, routes, ctx.source, ctx.path)
                record.framework = "aspnet"
        return record
