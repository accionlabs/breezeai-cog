"""ASP.NET Web Forms page/control detection.

A Web Forms page (``Foo.aspx``) and user control (``Foo.ascx``) are the app's user-facing
entry points, but — unlike MVC controllers or minimal APIs — they carry no routing
*construct* in the code-behind (IIS resolves the physical ``.aspx`` path). So this detector
is convention-based: for each code-behind file it emits ONE synthetic, file-parented
``route`` statement, mirroring how the React detector treats config/markup routes.

* ``.aspx.cs`` → ``routeKind=page``   (a navigable page)
* ``.ascx.cs`` → ``routeKind=mount``  (a user control — the Target Spec's current form)
* ``.master.cs`` → no record          (a layout, not a route)

A page or control composed into a master page also gets a ``routeKind=layout`` record
(:func:`detect_master_layout`), and a host markup file a ``mount`` record per control it
registers (the parser) — the Target Spec's current form for these links. The same links are
also in ``importFiles``.

``endpoint`` is derived from the code-behind path (``CMS/Enrollment.aspx.cs`` →
``/CMS/Enrollment.aspx``); ``handler`` is the code-behind class. Because no AST node backs
the route, ``nodeType`` is a synthetic marker (like the controller detector's ``attribute``).
Accurate URLs (``MapPageRoute``), page→control mount edges, and navigation are a phase-2
markup pass and intentionally out of scope here."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import file_id
from ...schemas import Class, FileRecord, Function
from ..dotnet_common import simple_attr_name
from ..emits import Emit

#: code-behind suffix → (routeKind, nodeType). nodeType is the shared ``synthetic`` sentinel
#: (no backing AST node — the markup isn't parsed); the page/control distinction lives in routeKind.
_KIND_BY_SUFFIX: dict[str, tuple[str, str]] = {
    ".aspx.cs": ("page", "synthetic"),
    ".ascx.cs": ("mount", "synthetic"),
}


def _endpoint(path: str) -> str:
    """Code-behind path → page URL: ``CMS/Enrollment.aspx.cs`` → ``/CMS/Enrollment.aspx``."""
    markup = path[: -len(".cs")]  # drop the trailing ".cs"
    return "/" + markup.lstrip("/")


def _page_class(record: FileRecord, path: str) -> Class | None:
    """The code-behind class — normally the markup file stem
    (``CMS/Enrollment.aspx.cs`` → class ``Enrollment``); fall back to the first class."""
    stem = path.rsplit("/", 1)[-1].split(".", 1)[0]  # Enrollment.aspx.cs → Enrollment
    by_name = {c.name: c for c in record.classes}
    return by_name.get(stem) or (record.classes[0] if record.classes else None)


def detect_webforms_pages(
    record: FileRecord, root: Node, path: str, page_routes: dict[str, list[str]] | None = None
) -> list[Emit]:
    """File-parented ``route`` record(s) for a Web Forms page, for :func:`~..emits.apply` —
    read off the record and the path, so the file's root stands in for a node and the span is
    set explicitly. A user control (``.ascx.cs``) is a ``routeKind="mount"`` record, the Target
    Spec's current form. Returns ``[]`` for anything else (a ``.master.cs``, a plain base-class
    file).

    When ``page_routes`` (the ``webforms.page-routes`` fact) maps this page's physical ``.aspx`` to friendly
    ``MapPageRoute`` URLs, the endpoint is **enriched** with the real routed URL(s) — one
    route per URL — instead of the physical path (BREEZEAI-765 item 2); otherwise a single
    physical-path route is emitted, as before."""
    match = next((kv for sfx, kv in _KIND_BY_SUFFIX.items() if path.endswith(sfx)), None)
    if match is None:
        return []
    route_kind, node_type = match
    cls = _page_class(record, path)
    start = cls.startLine if cls is not None else 1
    end = cls.endLine if cls is not None else 1

    # friendly MapPageRoute URLs win over the physical .aspx path (page routeKind only —
    # a mount/control isn't page-routed). Key is the physical .aspx repo path (path − ".cs").
    friendly = (page_routes or {}).get(path[: -len(".cs")]) if route_kind == "page" else None
    endpoints = [f"/{u.lstrip('/')}" for u in friendly] if friendly else [_endpoint(path)]

    out: list[Emit] = []
    for endpoint in endpoints:
        out.append(
            Emit(root, dict(
                semanticType="route",
                method="GET",
                endpoint=endpoint,
                framework="aspnet-webforms",
                routeKind=route_kind,
                handler=cls.name if cls is not None else None,
                # the page class is the handler: GET and every postback share the page URL,
                # the event handler is chosen by form data (decision Q6)
                handlerLine=start if cls is not None and route_kind == "page" else None,
            ), parent=file_id(path), node_type=node_type, text=path.rsplit("/", 1)[-1],
                line=start, col=0, end_line=end)
        )
    if route_kind == "page" and cls is not None:
        out += _page_methods(record, root, cls, endpoints)
    return out


#: ``[WebMethod]`` / ``[ScriptMethod]`` mark an ASP.NET AJAX page method (attribute short names).
_PAGE_METHOD_ATTRS = frozenset({"WebMethod", "ScriptMethod"})


def _page_methods(
    record: FileRecord, root: Node, cls: Class, endpoints: list[str],
) -> list[Emit]:
    """A static ``[WebMethod]`` on a page class is an ASP.NET AJAX *page method*: its own entry
    point, called with ``POST <page URL>/<Method>`` (JSON). One binding per page URL; the
    handler is the method itself (handlerLine its declaration, attributes included)."""
    out: list[Emit] = []
    methods: list[Function] = [
        fn for fn in record.functions
        if fn.parentId == cls.id and fn.isStatic
        and any(simple_attr_name(d.name) in _PAGE_METHOD_ATTRS for d in fn.decorators)
    ]
    for fn in methods:
        for endpoint in endpoints:
            out.append(Emit(root, dict(
                semanticType="route",
                method="POST",
                endpoint=f"{endpoint.rstrip('/')}/{fn.name}" if endpoint else None,
                framework="aspnet-webforms",
                routeKind="route",
                handler=fn.name,
                handlerLine=fn.startLine,
            ), parent=fn.id, node_type="synthetic", text=f"[WebMethod] {cls.name}.{fn.name}",
                line=fn.startLine, col=0, end_line=fn.endLine))
    return out


def detect_master_layout(
    record: FileRecord, root: Node, path: str, master_endpoint: str | None
) -> list[Emit]:
    """A ``routeKind="layout"`` record (the Target Spec's current form) saying that this page,
    control or master composes into ``master_endpoint`` (its ``MasterPageFile``); ``[]`` when there
    is no master. No HTTP ``method`` — a layout is a composition, not a served verb. The backend
    joins ``endpoint`` to the master's path for the page → master link; the link is also in
    ``importFiles``."""
    if master_endpoint is None:
        return []
    cls = _page_class(record, path)
    start = cls.startLine if cls is not None else 1
    end = cls.endLine if cls is not None else 1
    return [Emit(root, dict(
        semanticType="route",
        endpoint=master_endpoint,
        framework="aspnet-webforms",
        routeKind="layout",
        handler=cls.name if cls is not None else None,
    ), parent=file_id(path), node_type="synthetic", text=master_endpoint,
        line=start, col=1, end_line=end)]  # col 1 → distinct from the page record (col 0)
