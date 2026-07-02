"""AspNetCoreParser — a C# framework parser. Selected (one parser per file) over
CSharpParser when ``claims`` finds an ASP.NET namespace; reuses ``CSharpParser.extract``
(single parse), then detects controller routes (off the captured attributes) and
minimal-API ``app.MapGet(…)`` endpoints (AST walk). Covers ASP.NET Core MVC/Web API,
minimal APIs, and classic ASP.NET (System.Web)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .routes import detect_controller_routes, detect_minimal_api_routes

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
        if ctx.capture_statements:  # routes are statements — gated (spec A4)
            routes = detect_controller_routes(record)
            seen = {s.id for s in record.statements} | {r.id for r in routes}
            routes += detect_minimal_api_routes(
                root, ctx.source, ctx.path, seen,
                invocation_type="invocation_expression", member_type="member_access_expression",
            )
            if routes:
                record.statements.extend(routes)
                record.framework = "aspnet"
        return record
