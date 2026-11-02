"""Next.js **MDX pages** — ``app/**/page.mdx`` and ``pages/**/*.mdx``, when the app's
``pageExtensions`` lists ``mdx`` (as ``@next/mdx`` requires: without it Next.js routes no MDX
file)::

    // next.config.mjs
    const nextConfig = { pageExtensions: ['js', 'jsx', 'ts', 'tsx', 'md', 'mdx'] };
    app/about/page.mdx      → page /about
    pages/blog/post.mdx     → page /blog/post

An MDX file is Markdown with JSX: not TypeScript, and no grammar here parses it. So this parser
owns only the ``.mdx`` files at a Next.js page path (:meth:`NextMdxParser.matches` — any other
``.mdx``, a docs site's, is not a source file at all) and writes one ``route`` /
``routeKind: page`` record per page from its path alone: ``nodeType: synthetic`` (no syntax
node stands for it), ``text`` the file's first line, no ``handler`` (the page is its content,
not a named component).

The app's ``basePath``, ``pageExtensions`` and ``middleware`` / ``proxy`` guards apply as for the
other pages. Those come from TypeScript files (``next.config.*``, ``middleware.ts``), and the
index this parser gets is its own (the ``.mdx`` files'), so :meth:`NextMdxParser.build_index`
reads the few config files of each page's app itself, with the same fact collectors the
TypeScript index uses. A ``basePath`` held in a constant of another file does not resolve here
(the TypeScript constants are not read): the page's address is then unknown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...emit import file_id, statement_id
from ...schemas import FileRecord, Statement
from ...utils import count_loc, repo_relative
from ..base import BaseParser, ParseContext
from ..treesitter import first_line, parse_source
from ..typescript.base_paths import resolve_base_paths
from .facts import (
    APPS,
    BASE_PATHS,
    MIDDLEWARES,
    PAGE_EXTENSIONS,
    _collect_base_path,
    _collect_middleware,
    _collect_page_extensions,
    _is_next_app,
    _NEXT_CONFIGS,
    _resolve_middlewares,
    _resolve_page_extensions,
    next_apps,
    page_extensions,
    router_root,
)
from .pages import _app_router_endpoint, _pages_router_endpoint
from .routes import _middleware_guards, _served

_INTERCEPTORS = tuple(f"{stem}.{ext}" for stem in ("middleware", "proxy")
                      for ext in ("ts", "js", "mjs"))


@dataclass(frozen=True)
class NextMdxIndex:
    """The Next.js facts of the apps the MDX pages belong to (``facts`` as on the TS index)."""

    facts: dict[str, Any] = field(default_factory=dict)


def _router(path: str) -> str | None:
    """``app`` / ``pages`` — the router directory an ``.mdx`` file can be a page of — or None."""
    if _app_router_endpoint(path) is not None:
        return "app"
    dirs = path.replace("\\", "/").split("/")[:-1]
    if "pages" in dirs and dirs[dirs.index("pages") + 1 : dirs.index("pages") + 2] != ["api"]:
        return "pages"
    return None


class NextMdxParser(BaseParser):
    name = "nextjs-mdx"
    extensions = (".mdx",)
    frameworks = ["nextjs"]
    statement_types = ["route"]

    def matches(self, path: str | Path) -> bool:
        p = str(path)
        return p.endswith(".mdx") and _router(p) is not None

    def build_index(self, repo_root: Path, files: Sequence[Path], jobs: int = 1) -> NextMdxIndex:
        roots = {r for f in files
                 if (router := _router(rel := repo_relative(f, repo_root))) is not None
                 and (r := router_root(rel, router)) is not None}
        base_paths, middlewares, extensions = [], [], []
        for root in sorted(roots):
            app = Path(repo_root) / root if root else Path(repo_root)
            candidates = [app / c for c in _NEXT_CONFIGS] + [
                d / f for d in (app, app / "src") for f in _INTERCEPTORS]
            for f in candidates:
                if not f.is_file():
                    continue
                rel = repo_relative(f, repo_root)
                source = f.read_bytes()
                tree = parse_source("typescript", source, 0).root_node
                if (bp := _collect_base_path(tree, source, rel, Path(repo_root))) is not None:
                    base_paths.append(bp)
                if (ext := _collect_page_extensions(tree, source, rel, Path(repo_root))):
                    extensions.append(ext)
                if (mw := _collect_middleware(tree, source, rel, Path(repo_root))) is not None:
                    middlewares.append(mw)
        facts: dict[str, Any] = {
            APPS: frozenset(r for r in roots if _is_next_app(str(repo_root), r)),
            BASE_PATHS: resolve_base_paths(base_paths, None),
            MIDDLEWARES: _resolve_middlewares(middlewares, None),
            PAGE_EXTENSIONS: _resolve_page_extensions(extensions, None),
        }
        return NextMdxIndex({k: v for k, v in facts.items() if v})

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        text = ctx.source.decode("utf-8", "replace")
        fid = file_id(ctx.path)
        record = FileRecord(id=fid, path=ctx.path, type="code", language="mdx",
                            loc=count_loc(text))
        if not ctx.capture_statements or self.is_fixture_file(ctx.path):
            return record
        index = ctx.resolution_index
        router = _router(ctx.path)
        if router is None or router_root(ctx.path, router) not in next_apps(index):
            return record
        # An MDX file is a page only when the app lists `mdx` as a page extension.
        exts = page_extensions(index, ctx.path)
        if not exts or "mdx" not in exts:
            return record
        local = (_app_router_endpoint(ctx.path, exts) if router == "app"
                 else _pages_router_endpoint(ctx.path, exts))
        if local is None:
            return record
        last = max(text.count("\n") + (0 if text.endswith("\n") else 1), 1)
        record.framework = "nextjs"
        record.statements.append(Statement(
            id=statement_id(ctx.path, 1, 0),
            parentId=fid,
            nodeType="synthetic",
            semanticType="route",
            text=first_line(text)[:200],
            endpoint=_served(local, ctx.path, index),
            framework="nextjs",
            routeKind="page",
            guards=_middleware_guards(index, ctx.path, local) or None,
            isRegex=False,
            startLine=1,
            endLine=last,
            path=ctx.path,
        ))
        return record
