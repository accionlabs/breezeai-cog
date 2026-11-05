"""Next.js repository facts, registered on the TypeScript index.

* ``next.apps`` — the app roots (``""`` = the repo root) that are Next.js apps: the root of a
  file under ``app/`` or ``pages/`` (its parent when that is ``src``) holding a
  ``next.config.*``, or whose ``package.json`` depends on ``next``. A file-based page counts
  only inside one of these — Gatsby also routes ``src/pages/*.tsx``, and Angular has
  ``src/app``.
* ``next.middlewares`` — ``middleware.(ts|js)``, or ``proxy.(ts|js)`` (its Next.js 16 name):
  app directory → (the file's name — ``middleware`` / ``proxy``, the guard its routes get —
  its ``config.matcher`` patterns, or ``None`` when it has no matcher: runs on every route). A
  matcher that cannot be read (non-literal, a regex, has / missing conditions) contributes no
  pattern; an app directory with two such files maps to ``()`` patterns (covers nothing for
  certain). The app directory is the file's directory (its parent when that is ``src``)::

      // apps/web/src/proxy.ts                     → {"apps/web": ("proxy", ("/admin/:path*",))}
      export function proxy(req) { … }
      export const config = { matcher: ["/admin/:path*"] };

* ``next.base-paths`` — app directory (a ``next.config.*`` file's) → its ``basePath``
  (:mod:`..base_paths_common`, shared with LoopBack). Every config file counts, one that
  sets no ``basePath`` too: it marks its directory as an app.
* ``next.page-extensions`` — app directory → its ``pageExtensions``: the file endings that make
  a Pages Router file a page (``['page.tsx', 'api.ts']``: ``index.page.tsx`` is ``/``,
  ``OrderRow.tsx`` is a component). ``None`` when it is set but not a literal array of strings
  (a variable, a spread): which files are pages is unknown. Only configs that set it count; an
  app without one keeps Next.js's default (any ``.tsx`` / ``.ts`` / ``.jsx`` / ``.js`` file)::

      // next.config.js                            → {"": ("page.tsx", "api.ts")}
      module.exports = { pageExtensions: ['page.tsx', 'api.ts'] };
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..treesitter import node_text
from ..base_paths_common import BasePathRaw, collect_base_path, resolve_base_paths
from ..typescript.imports import _load_jsonc, _string_literal

APPS = "next.apps"
MIDDLEWARES = "next.middlewares"
BASE_PATHS = "next.base-paths"
PAGE_EXTENSIONS = "next.page-extensions"


_NEXT_CONFIGS = ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs")
#: The request-interceptor file → the name it exports its function under (and the guard name).
_MIDDLEWARE_FILES = {f"{stem}.{ext}": stem for stem in ("middleware", "proxy")
                     for ext in ("ts", "js", "mjs")}
#: app directory → (guard name, matcher patterns — None: every route).
Middleware = tuple[str, tuple[str, ...] | None]


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


def next_middlewares(index: Any | None) -> dict[str, Middleware]:
    return index_fact(index, MIDDLEWARES) or {}


def next_base_paths(index: Any | None) -> dict[str, str | None]:
    return index_fact(index, BASE_PATHS) or {}


def next_page_extensions(index: Any | None) -> dict[str, tuple[str, ...] | None]:
    return index_fact(index, PAGE_EXTENSIONS) or {}


def page_extensions(index: Any | None, path: str) -> tuple[str, ...] | None:
    """The ``pageExtensions`` of the app a ``path`` belongs to — the deepest directory above it
    holding a ``next.config``: ``()`` when that config sets none (Next.js's default), None when
    it is set but unknown."""
    configs = next_base_paths(index)
    anchors = [d for d in configs if d == "" or path.startswith(d + "/")]
    if not anchors:
        return ()
    return next_page_extensions(index).get(max(anchors, key=len), ())


def page_stem(filename: str, extensions: tuple[str, ...]) -> str | None:
    """A Pages Router file's name without its page extension (the longest that matches):
    ``index.page.tsx`` with ``page.tsx`` → ``index``; None when no extension matches (not a
    page). ``()`` (none configured): the file's own extension."""
    if not extensions:
        return filename.rsplit(".", 1)[0]
    matches = [e for e in extensions if filename.endswith("." + e)]
    if not matches:
        return None
    return filename[: -len(max(matches, key=len)) - 1]


def _collect_page_extensions(
    root: Node, source: bytes, rel: str, repo_root: Path | None,
) -> tuple[str, tuple[str, ...] | None] | None:
    """(the config's directory, its ``pageExtensions``) of a ``next.config.*`` that sets it."""
    if not rel.rsplit("/", 1)[-1].startswith("next.config."):
        return None
    found: list[tuple[str, ...] | None] = []
    consts: dict[str, Node] = {}  # top-level `const name = <value>`, for `{ pageExtensions }`
    for node in root.named_children:
        if node.type == "export_statement":
            node = node.child_by_field_name("declaration") or node
        if node.type == "lexical_declaration" and node_text(node, source).startswith("const"):
            for vd in node.named_children:
                name, value = vd.child_by_field_name("name"), vd.child_by_field_name("value")
                if name is not None and value is not None and name.type == "identifier":
                    consts[node_text(name, source)] = value

    def literal(value: Node | None) -> tuple[str, ...] | None:
        """A literal array of strings → its extensions; anything else is unknown."""
        if value is None or value.type != "array":
            return None
        items = [_string_literal(c, source) for c in value.named_children if c.type != "comment"]
        return tuple(i.lstrip(".") for i in items if i is not None) \
            if items and None not in items else None

    def walk(n: Node) -> None:
        if n.type == "pair":
            key, value = n.child_by_field_name("key"), n.child_by_field_name("value")
            if key is not None and value is not None \
                    and node_text(key, source).strip("'\"") == "pageExtensions":
                found.append(literal(consts.get(node_text(value, source))
                                     if value.type == "identifier" else value))
        elif n.type == "shorthand_property_identifier" \
                and node_text(n, source) == "pageExtensions":
            found.append(literal(consts.get("pageExtensions")))  # `{ pageExtensions }`
        elif n.type == "assignment_expression":
            left = n.child_by_field_name("left")
            # `cfg.pageExtensions = …` / `cfg['pageExtensions'] = …`: set at run time → unknown
            if left is not None and left.type in ("member_expression", "subscript_expression") \
                    and node_text(left, source).rstrip("]'\"").endswith("pageExtensions"):
                found.append(None)
        for c in n.named_children:
            walk(c)

    walk(root)
    if not found:
        return None
    anchor = PurePosixPath(rel).parent.as_posix()
    return ("" if anchor == "." else anchor), (found[0] if len(set(found)) == 1 else None)


def _resolve_page_extensions(
    values: list[tuple[str, tuple[str, ...] | None]], index: Any,
) -> dict[str, tuple[str, ...] | None] | None:
    out: dict[str, tuple[str, ...] | None] = {}
    for anchor, exts in values:
        out[anchor] = None if anchor in out and out[anchor] != exts else exts
    return out or None


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
) -> tuple[str, Middleware] | None:
    """A ``middleware.(ts|js)`` / ``proxy.(ts|js)`` that exports its function
    (``export function middleware`` / ``export const proxy`` / ``export default``) → (app
    directory, (its name, its matcher patterns)); None for any other file. No ``config.matcher``
    → ``None`` patterns (every route)."""
    kind = _MIDDLEWARE_FILES.get(rel.rsplit("/", 1)[-1])
    if kind is None:
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
                exported = exported or (name is not None and node_text(name, source) == kind)
            if decl.type not in ("lexical_declaration", "variable_declaration"):
                continue
            for vd in decl.named_children:
                name, value = vd.child_by_field_name("name"), vd.child_by_field_name("value")
                if name is None or value is None:
                    continue
                if node_text(name, source) == kind:
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
    app_dir = "" if str(app) == "." else str(app)
    # Only the file at an app's root (or its `src/`) is the interceptor: an `app/api/proxy.ts`
    # helper is an ordinary module. The app root is checked in its own directory, not by walking
    # up, so a nested helper never borrows the repo's `next` dependency.
    if repo_root is None or not _is_next_app(str(repo_root), app_dir):
        return None
    if kind == "proxy":
        major = _next_major(repo_root, app_dir)
        if major is None or major < 16:
            return None  # before Next.js 16 (or unknown) a `proxy.ts` is an ordinary module
    return app_dir, (kind, matcher)


def _next_major(repo_root: Path | None, app_dir: str) -> int | None:
    """The major version of ``next`` the app declares (its ``package.json``, else the nearest
    one above it), or None when it is not a plain version (``latest``, ``canary``, a workspace
    link) or there is none."""
    if repo_root is None:
        return None
    parts = PurePosixPath(app_dir).parts if app_dir else ()
    for depth in range(len(parts), -1, -1):
        pkg_path = Path(repo_root, *parts[:depth], "package.json")
        pkg = _load_jsonc(pkg_path) if pkg_path.is_file() else None
        if not isinstance(pkg, dict):
            continue
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        spec = deps.get("next")
        if isinstance(spec, str):
            m = re.match(r"^[\^~>=v\s]*(\d+)(?:\.|$)", spec)
            return int(m.group(1)) if m else None
    return None


def _resolve_middlewares(
    values: list[tuple[str, Middleware]], index: Any,
) -> dict[str, Middleware] | None:
    out: dict[str, Middleware] = {}
    for app, (kind, matcher) in values:
        out[app] = (kind, ()) if app in out else (kind, matcher)
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
register_fact(FactSpec(
    name=PAGE_EXTENSIONS, language="typescript", gate=(b"pageExtensions",),
    collect=_collect_page_extensions, resolve=_resolve_page_extensions,
))
