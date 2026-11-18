"""Next.js **MDX pages** — an ``.mdx`` file Next.js serves as a page (``@next/mdx``).

An ``.mdx`` file is a page only when Next.js is told to serve MDX: the app's ``next.config``
lists it in ``pageExtensions`` (``['tsx', 'mdx']``, or a suffixed form such as ``'page.mdx'``).
Without that — Next.js's default serves only ``.tsx`` / ``.ts`` / ``.jsx`` / ``.js`` — or when
``pageExtensions`` is set but not a literal list, no ``.mdx`` file is a page. The rest is the
``.tsx`` page rules (:mod:`.pages`), on the same facts — no config file is read again:

* App Router ``app/**/page.mdx``, Pages Router ``pages/**/*.mdx`` outside ``pages/api``; not
  ``_app`` / ``_document`` / ``_error`` / ``404`` / ``500``, nor under a private folder ``_x``,
  an intercepting route ``(.)x`` or a parallel-route slot ``@x``;
* the file is inside a Next.js app (``next.apps``, which counts ``.mdx`` paths too), and the
  ``next.config`` it is read against is that app's own — a nested app without one does not
  take a parent folder's ``pageExtensions``;
* route groups ``(x)`` are dropped from the URL, dynamic segments stay literal (``[id]``),
  ``index`` is its folder, and the app's ``basePath`` goes in front;
* the app's ``middleware.ts`` / ``proxy.ts`` puts its name in ``guards`` when its matcher
  covers the page; ``authRequired`` is never set.

``app/about/page.mdx`` → one ``route`` record, ``routeKind: page``, ``endpoint: /about``. The
page is the file's content, not a named component, so it has no ``handler``; no syntax node
stands for it (it comes from the file path), so ``nodeType`` is ``synthetic``, on line 1, with
the file's first line as its ``text``.

The Next.js facts live on the TypeScript index; the MDX parser reads it as a related language.
"""

from __future__ import annotations

from ...emit import file_id
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from .facts import config_root, next_apps, page_extensions, router_root
from .pages import app_router_endpoint, pages_router_endpoint
from .routes import middleware_guards, served

#: Cap for the page record's one-line text, as for ``.tsx`` pages.
_TEXT_LIMIT = 200


def _mdx_extensions(exts: tuple[str, ...]) -> tuple[str, ...]:
    """The ``pageExtensions`` entries that make an ``.mdx`` file a page: ``mdx`` itself or a
    suffixed form (``page.mdx``)."""
    return tuple(e for e in exts if e == "mdx" or e.endswith(".mdx"))


def _page(path: str, index: object | None) -> tuple[str, str] | None:
    """(router directory, URL) of a Next.js MDX page, or None."""
    exts = page_extensions(index, path)
    if exts is None:
        return None  # set, but not a literal list: which files are pages is unknown
    mdx = _mdx_extensions(exts)
    if not mdx:
        return None  # Next.js serves .mdx only when pageExtensions lists it
    if (url := app_router_endpoint(path, mdx)) is not None:
        return "app", url
    if (url := pages_router_endpoint(path, mdx)) is not None:
        return "pages", url
    return None


def _first_line(source: bytes) -> str:
    text = source.decode("utf-8-sig", "replace")
    return text.split("\n", 1)[0].rstrip("\r")[:_TEXT_LIMIT]


def detect_nextjs_mdx_page(dc: DetectContext) -> Emit | None:
    """The page record for a Next.js MDX page, or None."""
    index = dc.ctx.related_indexes.get("typescript")
    if index is None:
        return None
    found = _page(dc.path, index)
    if found is None:
        return None
    router_dir, local = found
    app = router_root(dc.path, router_dir)
    if app not in next_apps(index):
        return None
    if config_root(index, dc.path) != app:
        # The pageExtensions read are another app's (a parent folder's next.config): this
        # app has no config of its own, so Next.js's default — no .mdx pages — applies.
        return None
    return Emit(
        dc.root,
        {
            "semanticType": "route",
            "endpoint": served(local, dc.path, index),
            "framework": "nextjs",
            "handler": None,
            "handlerLine": None,
            "guards": middleware_guards(index, dc.path, local) or None,
            "routeKind": "page",
            "isRegex": False,
        },
        parent=file_id(dc.path),
        node_type="synthetic",
        text=_first_line(dc.source),
        line=1,
        col=0,
        end_line=1,
    )


def _detect(dc: DetectContext) -> Detection | None:
    page = detect_nextjs_mdx_page(dc)
    return Detection([page], label="nextjs") if page is not None else None


register_detector(
    Detector(
        name="nextjs-mdx-pages",
        gate=Gate("mdx", order=10, skip_fixtures=True),
        detect=_detect,
        frameworks=("nextjs",),
    )
)
