"""Web Forms ``MapPageRoute`` URLs, the registered fact ``webforms.page-routes``.

A page's friendly URL is declared away from the page, usually in ``RouteConfig`` /
``Global.asax``::

    routes.MapPageRoute("edit", "cms/edit/{id}", "~/CMS/Edit.aspx")    ' App_Start/RouteConfig.vb

The fact maps the physical ``.aspx`` (repo-relative, ``~/`` resolved against the app root) to
its friendly URLs, sorted (independent of file order); :func:`~.pages.detect_webforms_pages`
records the page at those URLs instead of its physical path. Each language registers the fact
on its own index with a ``collect`` that reads its tree (``csharp_webforms/page_routes.py``,
``vb_webforms/page_routes.py``) and :func:`add_page_route` / :func:`resolve_page_routes` from
here, so a page's routes are those registered in its own language.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any

from ..additive import index_fact
from ..dotnet_common import web_app_root

FACT = "webforms.page-routes"


def page_routes(index: Any | None) -> dict[str, list[str]]:
    return index_fact(index, FACT) or {}


def app_root_of(rel: str, repo_root: Path | None) -> str:
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


def add_page_route(
    found: dict[str, list[str]], url: str | None, physical: str | None, app_root: str,
) -> None:
    """Record one ``MapPageRoute(name, url, physical)`` whose ``url`` and ``physical`` are
    string literals (``None`` otherwise: nothing is recorded)."""
    key = _physical_aspx_key(physical or "", app_root)
    if url is None or key is None:
        return
    urls = found.setdefault(key, [])
    if url not in urls:
        urls.append(url)


def resolve_page_routes(
    values: list[dict[str, list[str]]], index: Any,
) -> dict[str, list[str]] | None:
    """The fact's ``resolve``: every file's mappings merged, URLs sorted."""
    out: dict[str, set[str]] = {}
    for per_file in values:
        for key, urls in per_file.items():
            out.setdefault(key, set()).update(urls)
    return {key: sorted(urls) for key, urls in out.items()} or None
