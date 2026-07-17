"""Web Forms page→page navigation (markup pass, item 4).

Navigation is declared two ways:

* **code-behind** — ``Response.Redirect("~/Login.aspx")`` / ``Response.RedirectPermanent(…)`` /
  ``Server.Transfer("~/Confirm.aspx")`` (a real call → the route keeps its real node type);
* **markup** — ``NavigateUrl="~/Help.aspx"`` (HyperLink) / ``PostBackUrl="~/Submit.aspx"``
  (cross-page postback) attributes (no backing C# node → ``synthetic``).

Each becomes a ``routeKind=navigation`` route whose ``endpoint`` is the resolved internal
target page. The backend joins ``endpoint`` ↔ a page route's endpoint to draw the page-flow
edge. Honest-null: only literal, internal, ``.aspx`` targets (query/fragment stripped) are
kept — dynamic args, external URLs, and friendly-URL redirects resolve to nothing."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import FileRecord, Statement
from ..csharp.imports import _positional_args, _string_literal
from ..treesitter import node_text
from .mounts import _app_root, _to_repo_path, ci_resolve
from .routes import _page_class

#: code-behind redirect/transfer method → the receiver it must be called on (precision guard,
#: so an unrelated ``x.Transfer(…)`` doesn't false-match).
_REDIRECT_METHODS = {"Redirect": "Response", "RedirectPermanent": "Response", "Transfer": "Server"}
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
    rel = _to_repo_path(t, cur_dir, app_root)
    if rel is None or not rel.lower().endswith(".aspx"):  # page targets only
        return None
    actual = ci_resolve(repo_root, rel) if repo_root is not None else None
    return "/" + (actual or rel)


def _codebehind_navs(root: Node, source: bytes) -> list[tuple[str, Node]]:
    """(target-string, invocation-node) for each ``Response.Redirect``/``Server.Transfer``
    literal call. AST-based + receiver-checked (not regex) — comments/strings/same-named
    methods on other objects never match. Iterative walk (recursion-safe)."""
    out: list[tuple[str, Node]] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "invocation_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type == "member_access_expression":
                nm = fn.child_by_field_name("name")
                method = node_text(nm, source) if nm is not None else None
                want_recv = _REDIRECT_METHODS.get(method) if method else None
                recv = fn.child_by_field_name("expression")
                recv_tail = node_text(recv, source).rsplit(".", 1)[-1] if recv is not None else ""
                if want_recv is not None and recv_tail == want_recv:
                    args = _positional_args(n)
                    target = _string_literal(args[0], source) if args else None
                    if target is not None:
                        out.append((target, n))
        stack.extend(n.named_children)
    return out


def detect_navigation(
    record: FileRecord, path: str, root: Node, source: bytes, markup: bytes,
    repo_root: Path | None,
) -> list[Statement]:
    """``routeKind=navigation`` route statements for this page's outgoing navigation (item 4).
    Code-behind redirects keep their real node type/line; markup ``NavigateUrl``/``PostBackUrl``
    are ``synthetic`` (no C# node), anchored to the page class."""
    app_root = _app_root(path, repo_root) if repo_root is not None else ""
    cur_dir = posixpath.dirname(path)
    cls = _page_class(record, path)
    cname = cls.name if cls is not None else None
    cstart = cls.startLine if cls is not None else 1
    cend = cls.endLine if cls is not None else 1
    seen = {s.id for s in record.statements}
    dedup: set[tuple[str, str, int]] = set()
    out: list[Statement] = []

    def emit(endpoint: str, node_type: str, start: int, end: int) -> None:
        key = (node_type, endpoint, start)
        if key in dedup:
            return
        dedup.add(key)
        stmt = Statement(
            id=disambiguate(statement_id(path, start, 2), seen),  # col 2 → distinct from page(0)/layout(1)
            parentId=file_id(path),
            nodeType=node_type,
            semanticType="route",
            text=endpoint,
            endpoint=endpoint,
            framework="aspnet-webforms",
            routeKind="navigation",
            handler=cname,
            startLine=start,
            endLine=end,
            path=path,
        )
        seen.add(stmt.id)
        out.append(stmt)

    if b"Redirect" in source or b"Transfer" in source:  # cheap gate before the AST walk
        for target, node in _codebehind_navs(root, source):
            ep = _nav_endpoint(target, cur_dir, app_root, repo_root)
            if ep is not None:
                emit(ep, node.type, node.start_point[0] + 1, node.end_point[0] + 1)
    for raw in _NAV_ATTR.findall(markup):
        ep = _nav_endpoint(raw.decode("utf-8", "replace"), cur_dir, app_root, repo_root)
        if ep is not None:
            emit(ep, "synthetic", cstart, cend)
    return out
