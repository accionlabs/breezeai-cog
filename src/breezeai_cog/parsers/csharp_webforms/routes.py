"""ASP.NET Web Forms page/control detection.

A Web Forms page (``Foo.aspx``) and user control (``Foo.ascx``) are the app's user-facing
entry points, but — unlike MVC controllers or minimal APIs — they carry no routing
*construct* in the code-behind (IIS resolves the physical ``.aspx`` path). So this detector
is convention-based: for each code-behind file it emits ONE synthetic, file-parented
``route`` statement, mirroring how the React detector treats config/markup routes.

* ``.aspx.cs`` → ``routeKind=page``   (a navigable page)
* ``.ascx.cs`` → ``routeKind=mount``  (a composable UI fragment / user control)
* ``.master.cs`` → skipped            (a layout, not a route)

``endpoint`` is derived from the code-behind path (``CMS/Enrollment.aspx.cs`` →
``/CMS/Enrollment.aspx``); ``handler`` is the code-behind class. Because no AST node backs
the route, ``nodeType`` is a synthetic marker (like the controller detector's ``attribute``).
Accurate URLs (``MapPageRoute``), page→control mount edges, and navigation are a phase-2
markup pass and intentionally out of scope here."""

from __future__ import annotations

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Class, FileRecord, Statement

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
    record: FileRecord, path: str, page_routes: dict[str, list[str]] | None = None
) -> list[Statement]:
    """Emit file-parented ``route`` statement(s) for a Web Forms page / user control.
    Returns ``[]`` for anything else (e.g. ``.master.cs`` or a plain base-class file).

    When ``page_routes`` (from the C# index) maps this page's physical ``.aspx`` to friendly
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
    seen = {s.id for s in record.statements}

    # friendly MapPageRoute URLs win over the physical .aspx path (page routeKind only —
    # a mount/control isn't page-routed). Key is the physical .aspx repo path (path − ".cs").
    friendly = (page_routes or {}).get(path[: -len(".cs")]) if route_kind == "page" else None
    endpoints = [f"/{u.lstrip('/')}" for u in friendly] if friendly else [_endpoint(path)]

    out: list[Statement] = []
    for endpoint in endpoints:
        out.append(Statement(
            id=disambiguate(statement_id(path, start, 0), seen),
            parentId=file_id(path),
            nodeType=node_type,
            semanticType="route",
            text=path.rsplit("/", 1)[-1],
            method="GET",
            endpoint=endpoint,
            framework="aspnet-webforms",
            routeKind=route_kind,
            handler=cls.name if cls is not None else None,
            startLine=start,
            endLine=end,
            path=path,
        ))
    return out


def detect_master_layout(record: FileRecord, path: str, master_endpoint: str | None) -> list[Statement]:
    """A ``routeKind=layout`` route statement recording that this page/control/master composes
    into ``master_endpoint`` (its ``MasterPageFile``). Returns ``[]`` when there is no master
    (BREEZEAI-765 item 3). No HTTP ``method`` — a layout is a composition, not a served verb;
    the backend joins ``endpoint`` ↔ the master's path to materialise the page→master link."""
    if master_endpoint is None:
        return []
    cls = _page_class(record, path)
    start = cls.startLine if cls is not None else 1
    end = cls.endLine if cls is not None else 1
    seen = {s.id for s in record.statements}
    return [Statement(
        id=disambiguate(statement_id(path, start, 1), seen),  # col 1 → distinct from the page route (col 0)
        parentId=file_id(path),
        nodeType="synthetic",
        semanticType="route",
        text=master_endpoint,
        endpoint=master_endpoint,
        framework="aspnet-webforms",
        routeKind="layout",
        handler=cls.name if cls is not None else None,
        startLine=start,
        endLine=end,
        path=path,
    )]
