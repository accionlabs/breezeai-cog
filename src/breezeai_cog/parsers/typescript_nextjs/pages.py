"""Next.js **page** detection — the UI entry points of both routers:

* **App Router** — ``app/**/page.(tsx|jsx|ts|js)``: the page's URL is its directory path.
* **Pages Router** — ``pages/**`` outside ``pages/api``: the URL is the file path from ``pages``
  (``index`` collapses to its directory). ``_app`` / ``_document`` / ``_error`` wrap every page
  and ``404`` / ``500`` are error screens, not URLs the app defines — none is a page. With
  ``pageExtensions`` in ``next.config`` (the ``next.page-extensions`` fact) only files ending in
  one of them are pages, the extension dropped from the URL (``index.page.tsx`` → ``/``); set
  but not a literal list → which files are pages is unknown, so the app's Pages Router gives
  no page records. The App Router follows it too: a page is ``page.`` + one of them
  (``page.page.tsx`` with ``['page.tsx']``).

Each page is one ``route`` record with ``routeKind: page``; ``handler`` is the default-exported
component (wrappers such as ``memo(Page)`` unwrapped), ``handlerLine`` its declaration in this
file. Route groups ``(x)`` are dropped from the URL; dynamic segments stay literal (``[id]``),
as on the API-route records. ``layout`` / ``template`` / ``loading`` / ``error`` files are
wiring, not pages; a private folder ``_x``, an intercepting route ``(.)x`` and a parallel-route
slot ``@x`` are not addressable on their own, so they get no record — a slot's page is drawn
inside its parent's layout, at the parent's URL, which the parent's own ``page`` records.

**Additive** (like Express): a page file is usually owned by the React parser (it imports
``react``), so this runs for every TS file and records the page whatever parser owns it. Only
files inside a Next.js app (the ``next.apps`` fact) count — Gatsby also routes
``src/pages/*.tsx``. The app's ``basePath`` and ``middleware.ts`` / ``proxy.ts`` guards apply,
as for API routes."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ...schemas import FileRecord
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import first_line, node_text
from .facts import next_apps, page_extensions, page_stem, router_root
from .routes import _middleware_guards, _served, _url_segments

_PAGE_EXTS = (".tsx", ".jsx", ".ts", ".js")
_SPECIAL_PAGES = {"_app", "_document", "_error", "404", "500"}


def _app_router_endpoint(path: str, extensions: tuple[str, ...] | None = ()) -> str | None:
    """``app/settings/profile/page.tsx`` → ``/settings/profile``; None when the file is not an
    App Router page — ``page`` plus one of the app's ``extensions`` (``()``: none set, any; None:
    unknown) — or sits in a private (``_x``), intercepting (``(.)x``) or parallel-route slot
    (``@x``) folder."""
    segs = path.replace("\\", "/").split("/")
    stem = page_stem(segs[-1], extensions) if extensions is not None else None
    if stem != "page" or "app" not in segs[:-1]:
        return None
    dirs = segs[segs.index("app") + 1 : -1]
    if any(d.startswith(("_", "(.", "@")) for d in dirs):
        return None
    return _url_segments(dirs)


def _pages_router_endpoint(path: str, extensions: tuple[str, ...] | None) -> str | None:
    """``pages/orders/[id].tsx`` → ``/orders/[id]``; None for ``pages/api/**``, the special
    files (``_app``, ``_document``, ``_error``, ``404``, ``500``), a file without one of the
    app's ``extensions`` (``()``: any) and when they are unknown (None)."""
    segs = path.replace("\\", "/").split("/")
    if "pages" not in segs[:-1]:
        return None
    rest = segs[segs.index("pages") + 1 :]
    if rest[:1] == ["api"] and len(rest) > 1:
        return None
    stem = page_stem(rest[-1], extensions) if extensions is not None else None
    if stem is None or stem in _SPECIAL_PAGES \
            or stem.endswith((".d", ".test", ".spec", ".stories")):
        return None
    tail = [] if stem == "index" else [stem]
    return _url_segments(rest[:-1] + tail)


def _page_endpoint(path: str, index: object | None) -> tuple[str, str] | None:
    """(router directory, URL) of a page file, or None."""
    if not path.endswith(_PAGE_EXTS):
        return None
    exts = page_extensions(index, path)
    if (url := _app_router_endpoint(path, exts)) is not None:
        return "app", url
    if (url := _pages_router_endpoint(path, exts)) is not None:
        return "pages", url
    return None


def _component(node: Node) -> Node | None:
    """The component a default export renders: unwrap ``memo(Page)`` / ``withAuth(Page)``
    to the innermost argument."""
    while node.type == "call_expression":
        args = node.child_by_field_name("arguments")
        inner = list(args.named_children) if args is not None else []
        if not inner:
            return None
        node = inner[-1]
    return node


def _default_export(root: Node) -> tuple[Node, Node] | None:
    """(export statement, exported value) of the module's default export, or None."""
    for child in root.named_children:
        if child.type != "export_statement" or not any(c.type == "default" for c in child.children):
            continue
        value = next((c for c in child.named_children if c.type not in ("comment", "decorator")), None)
        if value is not None:
            return child, value
    return None


def _handler(
    value: Node, source: bytes, record: FileRecord, fid: str
) -> tuple[str | None, int | None]:
    """(handler, handlerLine) of a page's default export: a named function / class → its name
    and declaration line; an identifier → its name, with the line when it is declared in this
    file; an anonymous function → no name, the line where it starts."""
    node = _component(value)
    if node is None:
        return None, None
    if node.type in ("arrow_function", "function_expression", "function"):
        return None, node.start_point[0] + 1
    if node.type in ("function_declaration", "class_declaration", "class"):
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return None, node.start_point[0] + 1
        return node_text(name_node, source), node.start_point[0] + 1
    if node.type == "identifier":
        name = node_text(node, source)
        lines = [f.startLine for f in record.functions if f.name == name and f.parentId == fid]
        lines += [c.startLine for c in record.classes if c.name == name]
        return name, lines[0] if len(lines) == 1 else None
    return None, None


def detect_nextjs_page(dc: DetectContext) -> Emit | None:
    """The page record for a Next.js page file, or None."""
    found = _page_endpoint(dc.path, dc.ctx.resolution_index)
    if found is None:
        return None
    router_dir, local = found
    if router_root(dc.path, router_dir) not in next_apps(dc.ctx.resolution_index):
        return None
    export = _default_export(dc.root)
    if export is None:
        return None  # Next.js renders a page's default export; without one it is not a page
    stmt_node, value = export
    fid = file_id(dc.path)
    handler, handler_line = _handler(value, dc.source, dc.record, fid)
    index = dc.ctx.resolution_index
    line = stmt_node.start_point[0] + 1
    parent = next(
        (f.id for f in dc.record.functions
         if handler and f.name == handler and f.parentId == fid),
        fid,
    )
    return Emit(
        stmt_node,
        {
            "semanticType": "route",
            "endpoint": _served(local, dc.path, index),
            "framework": "nextjs",
            "handler": handler,
            "handlerLine": handler_line,
            "guards": _middleware_guards(index, dc.path, local) or None,
            "routeKind": "page",
            "isRegex": False,
        },
        enrich="same",  # an existing statement on the default export becomes the page
        parent=parent, line=line,
        text=first_line(node_text(stmt_node, dc.source))[:200],
    )


def _detect(dc: DetectContext) -> Detection | None:
    page = detect_nextjs_page(dc)
    return Detection([page], label="nextjs") if page is not None else None


register_detector(Detector(
    name="nextjs-pages", gate=Gate("typescript", order=60, skip_fixtures=True), detect=_detect,
    frameworks=("nextjs",),
))
