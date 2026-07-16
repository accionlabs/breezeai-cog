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
from .mounts import master_codebehind, read_sibling_markup, resolve_master, resolve_mounts
from .navigation import detect_navigation
from .routes import detect_master_layout, detect_webforms_pages

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
        markup = read_sibling_markup(ctx.abs_path)  # read once — shared by all markup passes
        master_ep = resolve_master(markup, ctx.path, ctx.repo_root)  # once — layout stmt + import edge
        if ctx.capture_statements:  # routes are statements — gated (spec A4)
            page_routes = getattr(ctx.resolution_index, "page_routes", None)
            routes = detect_webforms_pages(record, ctx.path, page_routes)
            if routes:
                record.statements.extend(routes)
                record.framework = "aspnet-webforms"
            # Master-page composition → routeKind=layout statement (item 3).
            layout = detect_master_layout(record, ctx.path, master_ep)
            if layout:
                record.statements.extend(layout)
                record.framework = "aspnet-webforms"
            # Page→page navigation → routeKind=navigation statements (item 4).
            nav = detect_navigation(record, ctx.path, root, ctx.source, markup, ctx.repo_root)
            if nav:
                record.statements.extend(nav)
                record.framework = "aspnet-webforms"
        # Cross-file IMPORTS edges (core field, NOT statement-gated): host→control mounts +
        # page→master composition. Deduped against existing imports, sorted for determinism.
        imports = resolve_mounts(markup, ctx.path, ctx.source, ctx.repo_root)
        master_cb = master_codebehind(master_ep, ctx.repo_root)
        if master_cb is not None:
            imports.append(master_cb)
        if imports:
            existing = set(record.importFiles)
            record.importFiles.extend(i for i in sorted(set(imports)) if i not in existing)
            record.framework = "aspnet-webforms"
        return record
