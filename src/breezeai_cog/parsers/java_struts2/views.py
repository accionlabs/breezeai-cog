"""Where an action's results lead: views (JSP and other templates) and other actions.

A result is **navigation** (contract: a `route` with ``routeKind: navigation``): the action hands
the request on. Its ``endpoint`` is the target's address; a result whose address cannot be
resolved gives no navigation record (a link is never written with an empty endpoint):

=====================================  ===========================================================
Result type                            Target
=====================================  ===========================================================
``dispatcher`` (the default),          the view's context-relative path (``/WEB-INF/jsp/a.jsp``);
``freemarker``, ``velocity``,          a relative location is under the action's namespace
``plainText``                          (``/admin`` + ``a.jsp``); only a template file name counts
``redirect``                           the URL when it is a path (an absolute URL is not ours)
``redirectAction``, ``chain``          the action's address (``namespace`` param, else the
                                       action's own namespace)
anything else (``tiles``, ``json``,    nothing
``stream``, …)
=====================================  ===========================================================

The view file itself is found under the **web application root** — the directory holding
``WEB-INF`` — nearest the configuration or class (``src/main/webapp``, ``WebContent``,
``WebRoot``, ``web``; or the ``WEB-INF`` a configuration sits in). Two roots at the same level
resolve to none.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

from .common import action_endpoint

#: Web application folders, legacy layouts included ("" = the directory itself).
_WEB_DIRS = ("src/main/webapp", "WebContent", "WebRoot", "web", "webapp", "")
#: Result types whose location is a view template.
VIEW_TYPES = frozenset({"dispatcher", "freemarker", "velocity", "plainText"})
#: Result types whose location is another action.
ACTION_TYPES = frozenset({"redirectAction", "redirect-action", "chain"})
#: Template files a view result can name.
VIEW_EXTENSIONS = (".jsp", ".jspx", ".jspf", ".ftl", ".vm", ".html", ".htm")
#: The Convention plugin's default result folder (``struts.convention.result.path``).
CONVENTION_RESULT_PATH = "/WEB-INF/content/"


@dataclass(frozen=True, slots=True)
class Target:
    """Where one result leads: an address, and the view file in the repo when there is one."""

    endpoint: str | None
    file: str | None = None


def web_root(repo_root: Path, start: str) -> str | None:
    """The web application nearest the repo-relative directory ``start`` (see module doc)."""
    parts = [p for p in start.split("/") if p and p != "."]
    if "WEB-INF" in parts:
        return "/".join(parts[: parts.index("WEB-INF")])
    for depth in range(len(parts), -1, -1):
        base = "/".join(parts[:depth])
        hits = {"/".join(p for p in (base, d) if p)
                for d in _WEB_DIRS if (repo_root / base / d / "WEB-INF").is_dir()}
        if hits:
            return hits.pop() if len(hits) == 1 else None
    return None


def view_path(location: str, namespace: str | None) -> str | None:
    """The context-relative path of a view location: absolute as written, relative under the
    namespace. None for a placeholder, an OGNL expression, or a relative location with an
    unknown namespace."""
    location = location.strip().split("?", 1)[0]
    if not location or "${" in location or "%{" in location or "#{" in location:
        return None
    if location.startswith("/"):
        return location
    if namespace is None:
        return None
    return namespace.rstrip("/") + "/" + location


def view_file(repo_root: Path, root: str | None, path: str | None) -> str | None:
    """The repo file of a context-relative view path, when it exists."""
    if root is None or path is None:
        return None
    rel = posixpath.normpath(posixpath.join(root, path.lstrip("/")))
    if rel.startswith("../"):
        return None
    return rel if (repo_root / rel).is_file() else None


def result_target(kind: str, location: str | None, namespace: str | None,
                  target_namespace: str | None, suffix: str | None, repo_root: Path,
                  root: str | None) -> Target | None:
    """Where a result of type ``kind`` at ``location`` leads, or None when it is no navigation
    (an unknown type, a Tiles definition, a stream)."""
    if not location:
        return None
    if kind in VIEW_TYPES:
        if not location.split("?", 1)[0].lower().endswith(VIEW_EXTENSIONS):
            return None  # not a template file (a Tiles definition, a bean name)
        path = view_path(location, namespace)
        return Target(path, view_file(repo_root, root, path))
    if kind == "redirect":
        if "://" in location or location.startswith("//"):
            return None
        return Target(view_path(location, namespace))
    if kind in ACTION_TYPES:
        if "${" in location or "%{" in location:
            return Target(None)
        ns = target_namespace if target_namespace is not None else namespace
        return Target(action_endpoint(ns, location, suffix))
    return None
