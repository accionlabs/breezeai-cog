"""WebFormsParser — a C# framework parser for classic ASP.NET Web Forms. Selected (one
parser per file) over CSharpParser when ``claims`` sees a Web Forms code-behind — an
``.aspx.cs``/``.ascx.cs`` file, or a ``System.Web.UI`` import. Reuses
``CSharpParser.extract`` (single parse), then emits one file-parented ``route`` statement
per page (``.aspx.cs`` → ``routeKind=page``), mirroring the React detector (routes are
markup-level, not handler methods). A user control and a master page are parts of pages, not
entry points: their links stay structural (``importFiles``).

Endpoints are derived from the code-behind path. **Host→control mount edges** are resolved
by the markup pass (:mod:`.mounts`): ``<%@ Register Src %>`` from the sibling markup and
literal ``LoadControl("…")`` from the code-behind are resolved to each control's code-behind
path and added to ``importFiles`` (the ``IMPORTS`` edge). ``MapPageRoute`` friendly URLs,
master-page composition, and ``NavigateUrl`` navigation are later items of the same pass."""

from __future__ import annotations

from ...emit import file_id
from ...schemas import FileRecord
from ...utils import count_loc
from ..base import ParseContext
from ..csharp.parser import CSharpParser
from ..detection.jquery_calls import detect_inline_jquery
from ..emits import apply
from ..treesitter import parse_source
from .markup import (
    collect_markup_statements,
    resolve_codebehind,
    resolve_control_mounts,
    shadow_markup,
)
from .mounts import master_codebehind, read_sibling_markup, resolve_master, resolve_mounts
from .navigation import detect_navigation
from .page_routes import page_routes
from .routes import detect_webforms_pages

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
    template_extensions: tuple[str, ...] = _MARKUP_EXT  # the markup half only — .cs stays
    priority = 10  # framework parser > base csharp (0); disjoint claims from csharp-aspnet
    frameworks = ["aspnet-webforms"]
    # C# statement types + the markup nodes the Step-1 markup pass emits (capabilities honesty).
    statement_types = [*CSharpParser.statement_types, "element", "attribute"]

    def claims(self, path: str, source: bytes) -> bool:
        return (
            path.lower().endswith(_MARKUP_EXT)  # markup files (Step 1)
            or path.lower().endswith((".aspx.cs", ".ascx.cs", ".master.cs"))
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
        # Master-page composition: the <%@ Page MasterPageFile %> directive lives in this markup,
        # so link the markup file to its .master layout (markup→master IMPORTS edge), mirroring
        # the .ascx mount edges. resolve_master returns "/repo/rel/X.master" (or None).
        master = resolve_master(ctx.source, ctx.path, ctx.repo_root)
        if master is not None and master.lstrip("/") not in import_files:
            import_files.append(master.lstrip("/"))
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
            # inline <script> jQuery AJAX calls → api_call
            detect_inline_jquery(record, ctx.source, ctx.path, ctx.parse_timeout_micros)
        return record

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        if ctx.path.lower().endswith(_MARKUP_EXT):
            return self._parse_markup(ctx)
        root = parse_source("csharp", ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited C# extraction (one parse)
        markup = read_sibling_markup(ctx.abs_path)  # read once — shared by all markup passes
        master_ep = resolve_master(markup, ctx.path, ctx.repo_root)  # → the master import edge
        if ctx.capture_statements:  # routes are statements — gated (spec A4)
            routes = detect_webforms_pages(
                record, root, ctx.path, page_routes(ctx.resolution_index))
            apply(record, routes, ctx.source, ctx.path)
            # A page, a user control, or a file composed into a master page is Web Forms code.
            if routes or master_ep is not None or ctx.path.lower().endswith(".ascx.cs"):
                record.framework = "aspnet-webforms"
            # Page→page navigation → routeKind=navigation statements (item 4).
            nav = detect_navigation(record, ctx.path, root, ctx.source, markup, ctx.repo_root)
            if nav:
                apply(record, nav, ctx.source, ctx.path)
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
