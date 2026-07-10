"""VbAspNetParser — a VB.NET framework parser. Mirrors the C# ASP.NET parser: reuses
``VbParser.extract`` (single parse), then the **language-agnostic** controller-route
detector (:func:`detect_controller_routes` reads the ``FileRecord``, so it is shared
verbatim with C#) plus a VB-node minimal-API walk."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp_aspnet.routes import (
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
        if ctx.capture_statements:  # routes are statements — gated by --capture-statements
            routes = detect_controller_routes(record)
            seen = {s.id for s in record.statements} | {r.id for r in routes}
            routes += detect_minimal_api_routes(
                root, ctx.source, ctx.path, seen,
                invocation_type="invocation", member_type="member_access",
            )
            routes += detect_route_registrations(
                root, ctx.source, ctx.path, seen,
                invocation_type="invocation", member_type="member_access",
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "aspnet"
        return record
