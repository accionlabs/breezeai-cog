"""WebFormsParser — a C# framework parser for classic ASP.NET Web Forms. Selected (one
parser per file) over CSharpParser when ``claims`` sees a Web Forms code-behind — an
``.aspx.cs``/``.ascx.cs`` file, or a ``System.Web.UI`` import. Reuses
``CSharpParser.extract`` (single parse), then emits one file-parented ``route`` statement
per page (``.aspx.cs`` → ``routeKind=page``) or user control (``.ascx.cs`` → ``mount``),
mirroring the React detector (routes are markup-level, not handler methods).

Endpoints are derived from the code-behind path. **Host→control mount edges** are resolved
by the markup pass (:mod:`.mounts`): ``<%@ Register Src %>`` from the sibling markup and
literal ``LoadControl("…")`` from the code-behind are resolved to each control's code-behind
path and added to ``importFiles`` (the ``IMPORTS`` edge). ``MapPageRoute`` friendly URLs,
master-page composition, and ``NavigateUrl`` navigation are later items of the same pass."""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .mounts import resolve_mounts
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
            page_routes = getattr(ctx.resolution_index, "page_routes", None)
            routes = detect_webforms_pages(record, ctx.path, page_routes)
            if routes:
                record.statements.extend(routes)
                record.framework = "aspnet-webforms"
        # Host→control mounts → importFiles (IMPORTS edge). Not statement-gated: importFiles
        # is a core cross-file field, always emitted. Deduped against existing imports,
        # sorted additions for deterministic output.
        mounts = resolve_mounts(ctx.abs_path, ctx.path, ctx.source, ctx.repo_root)
        if mounts:
            existing = set(record.importFiles)
            record.importFiles.extend(m for m in mounts if m not in existing)
            record.framework = "aspnet-webforms"
        return record
