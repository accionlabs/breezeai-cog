"""Next.js repository facts, registered on the TypeScript index.

* ``next.apps`` — the app roots (``""`` = the repo root) that are Next.js apps: the root of a
  file under ``app/`` or ``pages/`` (its parent when that is ``src``) holding a
  ``next.config.*``, or whose ``package.json`` depends on ``next``. A file-based page counts
  only inside one of these — Gatsby also routes ``src/pages/*.tsx``, and Angular has
  ``src/app``.
* ``next.middlewares`` — ``middleware.(ts|js)``: app directory → its ``config.matcher``
  patterns, or ``None`` when it has no matcher (runs on every route). A matcher that cannot be
  read (non-literal, a regex, has / missing conditions) contributes no pattern; an app
  directory with two middleware files maps to ``()`` (covers nothing for certain). The app
  directory is the middleware file's directory (its parent when that is ``src``)::

      // apps/web/src/middleware.ts                → {"apps/web": ("/admin/:path*",)}
      export function middleware(req) { … }
      export const config = { matcher: ["/admin/:path*"] };

* ``next.base-paths`` — app directory (a ``next.config.*`` file's) → its ``basePath``
  (:mod:`..typescript.base_paths`, shared with LoopBack). Every config file counts, one that
  sets no ``basePath`` too: it marks its directory as an app.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..treesitter import node_text
from ..typescript.base_paths import BasePathRaw, collect_base_path, resolve_base_paths
from ..typescript.imports import _load_jsonc, _string_literal

APPS = "next.apps"
MIDDLEWARES = "next.middlewares"
BASE_PATHS = "next.base-paths"

_NEXT_CONFIGS = ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs")
_MIDDLEWARE_FILES = frozenset({"middleware.ts", "middleware.js", "middleware.mjs"})


def router_root(path: str, router_dir: str) -> str | None:
    """The app root of a file under a file-based ``router_dir`` (``app`` / ``pages``): the
    directory holding it, or its parent when that is ``src`` (``src/app``, ``src/pages``).
    ``""`` is the repo root; None when ``router_dir`` is not on the path."""
    dirs = path.replace("\\", "/").split("/")[:-1]
    if router_dir not in dirs:
        return None
    before = dirs[: dirs.index(router_dir)]
    if before and before[-1] == "src":
        before = before[:-1]
    return "/".join(before)


def next_apps(index: Any | None) -> frozenset[str]:
    return index_fact(index, APPS) or frozenset()


def next_middlewares(index: Any | None) -> dict[str, tuple[str, ...] | None]:
    return index_fact(index, MIDDLEWARES) or {}


def next_base_paths(index: Any | None) -> dict[str, str | None]:
    return index_fact(index, BASE_PATHS) or {}


def _collect_base_path(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> BasePathRaw | None:
    if not rel.rsplit("/", 1)[-1].startswith("next.config."):
        return None
    return collect_base_path(root, source, rel, "nextjs")


def _is_next_app(repo_root: str, root: str) -> bool:
    base = Path(repo_root) / root if root else Path(repo_root)
    if any((base / c).is_file() for c in _NEXT_CONFIGS):
        return True
    pkg = _load_jsonc(base / "package.json") if (base / "package.json").is_file() else None
    if not isinstance(pkg, dict):
        return False
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    return "next" in deps


def _collect_app(
    root: Node | None, source: bytes, rel: str, repo_root: Path | None,
) -> tuple[tuple[str, str], ...] | None:
    """(repo root, app root) for each router directory (``app`` / ``pages``) this file sits
    under; whether the root is a Next.js app is checked once per root, in resolve."""
    if repo_root is None:
        return None
    roots = tuple((str(repo_root), r) for d in ("app", "pages")
                  if (r := router_root(rel, d)) is not None)
    return roots or None


def _resolve_apps(values: list[tuple[tuple[str, str], ...]], index: Any) -> frozenset[str] | None:
    candidates = {pair for per_file in values for pair in per_file}
    return frozenset(r for repo, r in candidates if _is_next_app(repo, r)) or None


def _matcher_patterns(value: Node, source: bytes) -> tuple[str, ...]:
    """Literal ``config.matcher`` patterns: a string, an array of strings, or ``{ source }``
    objects without ``has`` / ``missing`` conditions. Anything else contributes no pattern."""
    items = value.named_children if value.type == "array" else [value]
    out: list[str] = []
    for item in items:
        if item.type == "object":
            pairs = {node_text(k, source).strip("'\""): pr.child_by_field_name("value")
                     for pr in item.named_children if pr.type == "pair"
                     and (k := pr.child_by_field_name("key")) is not None}
            if "has" in pairs or "missing" in pairs:
                continue
            item = pairs.get("source")  # type: ignore[assignment]
        lit = _string_literal(item, source)
        if lit is not None:
            out.append(lit)
    return tuple(out)


def _collect_middleware(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> tuple[str, tuple[str, ...] | None] | None:
    """A ``middleware.(ts|js)`` that exports its middleware (``export function middleware`` /
    ``export const middleware`` / ``export default``) → (app directory, its matcher patterns);
    None for any other file. No ``config.matcher`` → ``None`` patterns (every route)."""
    if rel.rsplit("/", 1)[-1] not in _MIDDLEWARE_FILES:
        return None
    exported = False
    matcher: tuple[str, ...] | None = None
    for node in root.named_children:
        if node.type != "export_statement":
            continue
        if any(c.type == "default" or node_text(c, source) == "default" for c in node.children):
            exported = True
        for decl in node.named_children:
            if decl.type == "function_declaration":
                name = decl.child_by_field_name("name")
                exported = exported or (name is not None and node_text(name, source) == "middleware")
            if decl.type not in ("lexical_declaration", "variable_declaration"):
                continue
            for vd in decl.named_children:
                name, value = vd.child_by_field_name("name"), vd.child_by_field_name("value")
                if name is None or value is None:
                    continue
                if node_text(name, source) == "middleware":
                    exported = True
                elif node_text(name, source) == "config":
                    matcher = ()  # a config that is not a literal object covers nothing for certain
                    for pair in value.named_children if value.type == "object" else []:
                        key = pair.child_by_field_name("key") if pair.type == "pair" else None
                        v = pair.child_by_field_name("value") if key is not None else None
                        if key is not None and node_text(key, source).strip("'\"") == "matcher" \
                                and v is not None:
                            matcher = _matcher_patterns(v, source)
                            break
                    else:
                        if value.type == "object":
                            matcher = None  # config without a matcher: every route
    if not exported:
        return None
    app = PurePosixPath(rel).parent
    if app.name == "src":
        app = app.parent
    return ("" if str(app) == "." else str(app)), matcher


def _resolve_middlewares(
    values: list[tuple[str, tuple[str, ...] | None]], index: Any,
) -> dict[str, tuple[str, ...] | None] | None:
    out: dict[str, tuple[str, ...] | None] = {}
    for app, matcher in values:
        out[app] = () if app in out else matcher
    return out or None


# Every file: where a file lives decides it, not what it says.
register_fact(FactSpec(
    name=APPS, language="typescript", gate=(b"",), collect=_collect_app, resolve=_resolve_apps,
))
register_fact(FactSpec(
    name=MIDDLEWARES, language="typescript", gate=(b"export",),
    collect=_collect_middleware, resolve=_resolve_middlewares,
))
# Every file: a config that sets no basePath still marks its app directory.
register_fact(FactSpec(
    name=BASE_PATHS, language="typescript", gate=(b"",),
    collect=_collect_base_path, resolve=resolve_base_paths,
))
