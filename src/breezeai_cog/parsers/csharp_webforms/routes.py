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


def detect_webforms_pages(record: FileRecord, path: str) -> list[Statement]:
    """Emit one file-parented ``route`` statement for a Web Forms page / user control.
    Returns ``[]`` for anything else (e.g. ``.master.cs`` or a plain base-class file)."""
    match = next((kv for sfx, kv in _KIND_BY_SUFFIX.items() if path.endswith(sfx)), None)
    if match is None:
        return []
    route_kind, node_type = match
    cls = _page_class(record, path)
    start = cls.startLine if cls is not None else 1
    end = cls.endLine if cls is not None else 1
    seen = {s.id for s in record.statements}
    return [
        Statement(
            id=disambiguate(statement_id(path, start, 0), seen),
            parentId=file_id(path),
            nodeType=node_type,
            semanticType="route",
            text=path.rsplit("/", 1)[-1],
            method="GET",
            endpoint=_endpoint(path),
            framework="aspnet-webforms",
            routeKind=route_kind,
            handler=cls.name if cls is not None else None,
            startLine=start,
            endLine=end,
            path=path,
        )
    ]
