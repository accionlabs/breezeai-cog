"""WebFormsParser — a C# framework parser for classic ASP.NET Web Forms. Selected (one
parser per file) over CSharpParser when ``claims`` sees a Web Forms code-behind — an
``.aspx.cs``/``.ascx.cs`` file, or a ``System.Web.UI`` import. Reuses
``CSharpParser.extract`` (single parse), then emits one file-parented ``route`` statement
per page (``.aspx.cs`` → ``routeKind=page``) or user control (``.ascx.cs`` → ``mount``),
mirroring the React detector (routes are markup-level, not handler methods).

Markup (``.aspx``/``.ascx``) itself is NOT parsed here, so the endpoint is derived from the
code-behind path and ``LoadControl`` mount edges / ``MapPageRoute`` friendly URLs /
``NavigateUrl`` navigation are out of scope — that is the phase-2 markup pass (see routes.py)."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .routes import detect_webforms_pages

#: Web Forms code-behind imports System.Web.UI (Page/UserControl) — NOT the MVC/Core
#: markers that select the sibling AspNetCoreParser, so the two claim disjoint files.
_MARKERS = (b"System.Web.UI",)


class WebFormsParser(CSharpParser):
    name = "csharp-webforms"
    priority = 10  # framework parser > base csharp (0); disjoint claims from csharp-aspnet
    frameworks = ["aspnet-webforms"]

    def claims(self, path: str, source: bytes) -> bool:
        return path.endswith((".aspx.cs", ".ascx.cs", ".master.cs")) or any(
            m in source for m in _MARKERS
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        if ctx.capture_statements:  # routes are statements — gated (spec A4)
            routes = detect_webforms_pages(record, ctx.path)
            if routes:
                record.statements.extend(routes)
                record.framework = "aspnet-webforms"
        return record
