"""Web Forms page→page navigation, for C# and VB code-behind.

Navigation is declared two ways:

* **code-behind** — ``Response.Redirect("~/Login.aspx")`` / ``Response.RedirectPermanent(…)`` /
  ``Server.Transfer("~/Confirm.aspx")`` (a real call → the route keeps its real node type).
  Each language reads these calls from its own tree (``csharp_webforms/navigation.py``,
  ``vb_webforms/navigation.py``) and hands them here as ``(target, node)`` pairs;
* **markup** — ``NavigateUrl="~/Help.aspx"`` (HyperLink) / ``PostBackUrl="~/Submit.aspx"``
  (cross-page postback) attributes (no backing code node → ``synthetic``).

Each becomes a ``routeKind=navigation`` route whose ``endpoint`` is the resolved internal
target page. The backend joins ``endpoint`` ↔ a page route's endpoint to draw the page-flow
edge. Honest-null: only literal, internal, ``.aspx`` targets (query/fragment stripped) are
kept — dynamic args, external URLs, and friendly-URL redirects resolve to nothing."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from tree_sitter import Node

from ...emit import file_id
from ...schemas import FileRecord
from ..dotnet_common import ci_resolve, to_repo_path, web_app_root
from ..emits import Emit
from .pages import _page_class

#: code-behind redirect/transfer method → the receiver it must be called on (precision guard,
#: so an unrelated ``x.Transfer(…)`` doesn't false-match).
REDIRECT_METHODS = {"Redirect": "Response", "RedirectPermanent": "Response", "Transfer": "Server"}
#: markup navigation attributes (server-control ``NavigateUrl`` / ``PostBackUrl``).
_NAV_ATTR = re.compile(rb"\b(?:NavigateUrl|PostBackUrl)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _nav_endpoint(target: str, cur_dir: str, app_root: str, repo_root: Path | None) -> str | None:
    """A navigation target → internal ``/…​.aspx`` endpoint, or ``None`` for external /
    client-side / non-page / friendly-URL targets. Query string and fragment are stripped.
    An in-repo target is case-corrected to its real casing (so the backend join lands); a
    target not in the tree is still emitted (navigation intent to an unscanned page)."""
    t = target.strip().replace("\\", "/").split("?", 1)[0].split("#", 1)[0]
    if t.lower().startswith(("http://", "https://", "mailto:", "javascript:", "//", "tel:")):
        return None
    rel = to_repo_path(t, cur_dir, app_root)
    if rel is None or not rel.lower().endswith(".aspx"):  # page targets only
        return None
    actual = ci_resolve(repo_root, rel) if repo_root is not None else None
    return "/" + (actual or rel)


def detect_navigation(
    record: FileRecord,
    path: str,
    root: Node,
    redirects: list[tuple[str, Node]],
    markup: bytes,
    repo_root: Path | None,
) -> list[Emit]:
    """``navigation`` records for this page's outgoing navigation, for :func:`~..emits.apply`.
    ``redirects`` are the code-behind's literal ``Response.Redirect`` / ``Server.Transfer``
    calls ``(target, call node)``, read by the language; they keep their real node type/line.
    Markup ``NavigateUrl``/``PostBackUrl`` are ``synthetic`` (no code node), anchored to the
    page class.
    All sit at column 2 (distinct from the page record at 0) and parent to the file."""
    app_root = web_app_root(path, repo_root) if repo_root is not None else ""
    cur_dir = posixpath.dirname(path)
    cls = _page_class(record, path)
    cname = cls.name if cls is not None else None
    cstart = cls.startLine if cls is not None else 1
    cend = cls.endLine if cls is not None else 1
    dedup: set[tuple[str, str, int]] = set()
    out: list[Emit] = []

    def emit(endpoint: str, node_type: str, start: int, end: int) -> None:
        key = (node_type, endpoint, start)
        if key in dedup:
            return
        dedup.add(key)
        out.append(Emit(
            root,
            {"semanticType": "route", "routeKind": "navigation", "endpoint": endpoint,
             "framework": "aspnet-webforms", "handler": cname},
            parent=file_id(path), node_type=node_type, text=endpoint,
            line=start, col=2, end_line=end,  # col 2 → distinct from page(0)/layout(1)
        ))

    for target, node in redirects:
        ep = _nav_endpoint(target, cur_dir, app_root, repo_root)
        if ep is not None:
            emit(ep, node.type, node.start_point[0] + 1, node.end_point[0] + 1)
    for raw in _NAV_ATTR.findall(markup):
        ep = _nav_endpoint(raw.decode("utf-8", "replace"), cur_dir, app_root, repo_root)
        if ep is not None:
            emit(ep, "synthetic", cstart, cend)
    return out
