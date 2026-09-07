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

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ...utils import count_loc
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..treesitter import parse_source
from .markup import (
    collect_markup_statements,
    resolve_codebehind,
    resolve_control_mounts,
    shadow_markup,
)
from .mounts import master_codebehind, read_sibling_markup, resolve_master, resolve_mounts
from .navigation import detect_navigation
from .routes import detect_master_layout, detect_webforms_pages

#: Web Forms code-behind imports System.Web.UI (Page/UserControl) — NOT the MVC/Core
#: markers that select the sibling AspNetCoreParser, so the two claim disjoint files.
_MARKERS = (b"System.Web.UI",)
#: Markup file suffixes this parser owns (Step 1). ``.aspx.cs`` etc. are code-behind (C#),
#: handled by the inherited path; these bare suffixes are the markup.
_MARKUP_EXT = (".aspx", ".ascx", ".master")


class WebFormsParser(CSharpParser):
    name = "csharp-webforms"
    # Own the markup files too, so ``.aspx``/``.ascx``/``.master`` get a File node (was skipped).
    extensions: tuple[str, ...] = (*CSharpParser.extensions, *_MARKUP_EXT)
    priority = 10  # framework parser > base csharp (0); disjoint claims from csharp-aspnet
    frameworks = ["aspnet-webforms"]
    # C# statement types + the markup nodes the Step-1 markup pass emits (capabilities honesty).
    statement_types = [*CSharpParser.statement_types, "element", "attribute"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            path.endswith(_MARKUP_EXT)  # markup files (Step 1)
            or path.endswith((".aspx.cs", ".ascx.cs", ".master.cs"))
            or any(m in source for m in _MARKERS)
        )

    def _parse_markup(self, ctx: ParseContext) -> FileRecord:
        """Step 1: a ``.aspx``/``.ascx``/``.master`` markup File — server-control declarations
        and ``On<Event>`` handler wiring, parsed from the island-blanked shadow."""
        fid = file_id(ctx.path)
        codebehind = resolve_codebehind(ctx.source, ctx.path, ctx.repo_root)
        # Host→control mounts declared in this markup (<%@ Register Src %>) → the control's
        # .ascx (a File node since Step 1). importFiles is the host→control IMPORTS edge.
        mounts = resolve_control_mounts(ctx.source, ctx.path, ctx.repo_root)
        import_files = [codebehind] if codebehind is not None else []
        import_files.extend(c for c, _ in mounts if c not in import_files)
        record = FileRecord(
            id=fid,
            path=ctx.path,
            type="code",
            language="html",
            framework="aspnet-webforms",
            uiRole="template",
            loc=count_loc(ctx.source.decode("utf-8", "replace")),
            importFiles=import_files,
        )
        if ctx.capture_statements:
            # Parse the shadow (islands blanked) but read text from the ORIGINAL source — offsets
            # are identical (blanking preserves length), so line/column and text stay true.
            root = parse_source(
                "html", shadow_markup(ctx.source), ctx.parse_timeout_micros
            ).root_node
            seen_ids: set[str] = set()
            record.statements.extend(
                collect_markup_statements(
                    root, ctx.source, ctx.path, fid, seen_ids, ctx.statement_text_limit
                )
            )
            # Host→control mount as a queryable route (host source, control endpoint) — a
            # directive-derived route with no backing AST node → nodeType=synthetic (spec).
            # Route emitters skip fixtures.
            if not self.is_fixture_file(ctx.path):
                for control, line in mounts:
                    record.statements.append(
                        Statement(
                            id=disambiguate(statement_id(ctx.path, line, 0), seen_ids),
                            parentId=fid,
                            nodeType="synthetic",
                            semanticType="route",
                            framework="aspnet-webforms",
                            routeKind="mount",
                            endpoint="/" + control,
                            text=f"mounts {control}",
                            startLine=line,
                            endLine=line,
                            path=ctx.path,
                        )
                    )
        return record

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        if ctx.path.endswith(_MARKUP_EXT):
            return self._parse_markup(ctx)
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        markup = read_sibling_markup(ctx.abs_path)  # read once — shared by all markup passes
        master_ep = resolve_master(
            markup, ctx.path, ctx.repo_root
        )  # once — layout stmt + import edge
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
