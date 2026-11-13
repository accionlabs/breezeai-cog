"""VbWebFormsParser — classic ASP.NET Web Forms with VB code-behind (``.aspx.vb``, ``.ascx.vb``,
``.master.vb``). Reuses ``VbParser.extract`` (single parse), then records what the C# Web Forms
parser records, through the same ``webforms_common`` code:

* a page (``Orders.aspx.vb``) → ``route``, ``routeKind: page``, ``GET``, at its physical path or
  its ``MapPageRoute`` URLs (the VB ``webforms.page-routes`` fact), ``handler`` = the page class;
* a ``<WebMethod()> Public Shared Function`` on a page → its own ``POST <page>/<Method>`` route;
* ``Response.Redirect`` / ``Server.Transfer`` and the markup's ``NavigateUrl`` / ``PostBackUrl`` →
  ``routeKind: navigation``;
* the controls the page mounts (``<%@ Register Src %>``, ``LoadControl("…")``) and its master page
  → ``importFiles`` (the control's and the master's code-behind).

Claimed by suffix only: a VB project imports ``System.Web.UI`` project-wide (``.vbproj``), so the
file often never names it, and a controller that does (``OutputCacheLocation``) is MVC. The
markup files themselves stay with the C# Web Forms parser, which owns ``.aspx``/``.ascx``/
``.master`` whatever the code-behind's language.
"""

from __future__ import annotations

from ...schemas import FileRecord
from ..base import ParseContext
from ..emits import apply
from ..vb.parser import VbParser
from ..vb.source import parse_vb
from ..webforms_common.mounts import (
    master_codebehind, read_sibling_markup, resolve_master, resolve_mounts,
)
from ..webforms_common.navigation import detect_navigation
from ..webforms_common.page_routes import page_routes
from ..webforms_common.pages import detect_webforms_pages
from .navigation import codebehind_redirects

_CODE_BEHIND = (".aspx.vb", ".ascx.vb", ".master.vb")


class VbWebFormsParser(VbParser):
    name = "vb-webforms"
    priority = 10  # framework parser > base vb (0); disjoint claims from vb-aspnet
    frameworks = ["aspnet-webforms"]

    def claims(self, path: str, source: bytes) -> bool:
        return path.lower().endswith(_CODE_BEHIND)

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_vb(ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited VB extraction (one parse)
        markup = read_sibling_markup(ctx.abs_path)  # read once — shared by all markup passes
        master_ep = resolve_master(markup, ctx.path, ctx.repo_root)
        if ctx.capture_statements:  # routes are statements — gated
            routes = detect_webforms_pages(
                record, root, ctx.path, page_routes(ctx.resolution_index))
            apply(record, routes, ctx.source, ctx.path)
            nav = detect_navigation(record, ctx.path, root,
                                    codebehind_redirects(root, ctx.source), markup, ctx.repo_root)
            if nav:
                apply(record, nav, ctx.source, ctx.path)
        # Cross-file IMPORTS edges (core field, NOT statement-gated): host→control mounts +
        # page→master composition. Deduped against existing imports, sorted for determinism.
        imports = resolve_mounts(markup, ctx.path, ctx.source, ctx.repo_root)
        master_cb = master_codebehind(master_ep, ctx.repo_root, ".vb")
        if master_cb is not None:
            imports.append(master_cb)
        if imports:
            existing = set(record.importFiles)
            record.importFiles.extend(i for i in sorted(set(imports)) if i not in existing)
        record.framework = "aspnet-webforms"  # every claimed file is a Web Forms code-behind
        return record
