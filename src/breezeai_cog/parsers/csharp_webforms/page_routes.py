"""Web Forms ``MapPageRoute`` URLs as a registered fact on the C# index, ``webforms.page-routes``.

A page's friendly URL is declared away from the page, usually in ``RouteConfig`` /
``Global.asax``::

    routes.MapPageRoute("edit", "cms/edit/{id}", "~/CMS/Edit.aspx");   // App_Start/RouteConfig.cs

The fact maps the physical ``.aspx`` (repo-relative, ``~/`` resolved against the app root) to
its friendly URLs, sorted (independent of file order); :func:`~.routes.detect_webforms_pages` records the page
at those URLs instead of its physical path.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..csharp.imports import _called_name, _positional_args, _string_literal
from ..dotnet_common import web_app_root

FACT = "webforms.page-routes"


def page_routes(index: Any | None) -> dict[str, list[str]]:
    return index_fact(index, FACT) or {}


def _aspx_app_root(rel: str, repo_root: Path | None) -> str:
    """Application root for a file in a Web Forms repo: the shallowest ancestor directory
    (repo-root downward) containing ``web.config``. Falls back to ``""`` (repo root) when
    ``repo_root`` is absent or no ``web.config`` is found."""
    return web_app_root(rel, repo_root) if repo_root is not None else ""


def _physical_aspx_key(physical: str, app_root: str = "") -> str | None:
    """A ``MapPageRoute`` physical arg → repo-relative ``.aspx`` key.

    ``~/CMS/E.aspx`` with ``app_root="SubDir/App"`` → ``SubDir/App/CMS/E.aspx``.
    When ``app_root`` is ``""`` (repo root = app root, or unknown), the result is just the
    ``~/``-stripped path — matching the pre-existing behaviour for flat repos."""
    p = physical.strip().replace("\\", "/")
    if p.startswith("~/"):
        p = p[2:]
    elif p.startswith("/"):
        p = p[1:]
    p = posixpath.normpath(p)
    if not p.lower().endswith(".aspx") or p.startswith(".."):
        return None
    return posixpath.join(app_root, p) if app_root else p


def _collect(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> dict[str, list[str]] | None:
    """Collect ``routes.MapPageRoute(name, url, "~/physical.aspx")`` mappings from the AST
    (not raw text — so matches in comments/strings are excluded). Only string-literal
    ``url``/``physical`` args are recorded; the physical ``.aspx`` keys the friendly url.

    ``rel`` + ``repo_root`` resolve ``~/`` relative to the **app root** (the shallowest
    ancestor directory that holds a ``web.config``), producing a full repo-relative key that
    matches the path used by ``detect_webforms_pages``. For repos where the app root equals
    the repo root (or when ``repo_root`` is absent), the key reduces to the bare ``.aspx``
    path — identical to the previous behaviour.

    Iterative walk (no recursion — deep ASTs can't blow the stack)."""
    app_root = _aspx_app_root(rel, repo_root)
    found: dict[str, list[str]] = {}
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "invocation_expression" and _called_name(n, source) == "MapPageRoute":
            args = _positional_args(n)
            if len(args) >= 3:
                url = _string_literal(args[1], source)
                key = _physical_aspx_key(_string_literal(args[2], source) or "", app_root)
                if url is not None and key is not None:
                    urls = found.setdefault(key, [])
                    if url not in urls:
                        urls.append(url)
        stack.extend(n.named_children)
    return found or None


def _resolve(values: list[dict[str, list[str]]], index: Any) -> dict[str, list[str]] | None:
    out: dict[str, set[str]] = {}
    for per_file in values:
        for key, urls in per_file.items():
            out.setdefault(key, set()).update(urls)
    return {key: sorted(urls) for key, urls in out.items()} or None


register_fact(FactSpec(
    name=FACT, language="csharp", gate=(b"MapPageRoute",), collect=_collect, resolve=_resolve,
))
