"""Lead a ``conf/routes`` binding to its controller method: ``handler``, ``handlerLine`` and, as
the controller is always another file, ``handlerPath``.

A routes line names the action by its fully-qualified path::

    # conf/routes
    GET   /    controllers.HomeController.index

    // app/controllers/HomeController.java
    package controllers;
    public class HomeController extends Controller {
        public Result index() { … }                       // line 4

    → handler "index", handlerPath "app/controllers/HomeController.java", handlerLine 4

The routes parser is its own base parser, so it has no Scala or Java index to ask. It reads the
controllers itself, in Play's project layout: a routes file is ``<project>/conf/…``, and its
controllers are compiled from ``<project>/app/``. A candidate is a ``<Class>.scala`` /
``<Class>.java`` file under ``app/`` (a Scala package need not match its folders); it counts only
when its ``package`` declaration is the route's package and it declares the class's method.
Found in no file, in two, or overloaded → no location (honest-null): Play's own
``controllers.Assets`` is not in the repo and stays as written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..handler_sites import method_sites
from ..treesitter import node_text, parse_source
from .includes import project_of
from .routes import route_lines

FACT = "play.route-handlers"

_LANGUAGES = {".scala": "scala", ".java": "java"}
_PACKAGE_NODES = {"scala": "package_clause", "java": "package_declaration"}

#: (routes file's project dir, the handler as written) → (file, line), or None when unknown.
RouteHandlers = dict[tuple[str, str], tuple[str, int] | None]


def split_handler(handler: str) -> tuple[str, str, str] | None:
    """``controllers.admin.Users.list`` → ``("controllers.admin", "Users", "list")``; a leading
    ``@`` (an injected controller instance) is dropped. None when it names no class."""
    parts = handler.lstrip("@").split(".")
    if len(parts) < 2 or not all(parts):
        return None
    return ".".join(parts[:-2]), parts[-2], parts[-1]


def _package(root: Node, source: bytes, language: str) -> str:
    # Scala may chain clauses (`package a` then `package b` = `a.b`); Java has one.
    names = []
    for child in root.named_children:
        if child.type == _PACKAGE_NODES[language]:
            name = next((c for c in child.named_children if c.type != "comment"), None)
            if name is not None:
                names.append(node_text(name, source).strip())
    return ".".join(names)


def _sites_in(path: Path, rel: str, package: str, cls: str,
              method: str) -> list[tuple[str, int]]:
    language = _LANGUAGES[path.suffix]
    try:
        source = path.read_bytes()
        root = parse_source(language, source).root_node
    except (OSError, ValueError):
        return []
    if _package(root, source, language) != package:
        return []
    return [site for key, site in method_sites(language, root, source, rel)
            if key == (cls, method)]


def collect_route_handlers(repo_root: Path, routes: list[tuple[str, str]]) -> RouteHandlers:
    """Locate every handler the routes files ``(rel path, text)`` name."""
    found: RouteHandlers = {}
    candidates: dict[tuple[str, str], list[Path]] = {}
    for rel, text in routes:
        project = project_of(rel)
        if project is None:
            continue
        app = repo_root / project / "app"
        for handler in route_lines(text):
            key = (project, handler)
            parsed = split_handler(handler)
            if key in found or parsed is None:
                continue
            package, cls, method = parsed
            if (project, cls) not in candidates:
                candidates[(project, cls)] = sorted(
                    p for ext in _LANGUAGES for p in app.rglob(f"{cls}{ext}") if p.is_file()
                ) if app.is_dir() else []
            sites = [site for p in candidates[(project, cls)]
                     for site in _sites_in(p, p.relative_to(repo_root).as_posix(),
                                           package, cls, method)]
            found[key] = sites[0] if len(sites) == 1 else None
    return found


def route_handler(index: Any | None, routes_rel: str,
                  handler: str) -> tuple[str, int] | None:
    """Where the handler a routes file names is declared, from the routes index."""
    facts = getattr(index, "facts", None) or {}
    project = project_of(routes_rel)
    table: RouteHandlers = facts.get(FACT) or {}
    return table.get((project, handler)) if project is not None else None
