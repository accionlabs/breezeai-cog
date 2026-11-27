"""The views a Razor view pulls in — its layout and its partials — as file links.

``Layout = "_Layout"``, ``<partial name="_Menu" />`` and ``@Html.Partial("_Footer")`` name a view
by its name, not its path. ASP.NET finds it by searching folders in a fixed order; this module
follows that order and links the first view that exists:

1. the referencing view's own folder;
2. a ``Shared`` folder in that folder, then in each parent folder, up to the ``Views`` / ``Pages``
   root (``Views/Shared``, ``Pages/Shared``);
3. the app-wide ``Pages/Shared`` and ``Views/Shared`` above that root, nearest first (a Razor
   Page, or a page in an ``Areas/<Name>`` folder, falls back to them).

A path (``~/Views/Shared/_Layout.cshtml``) is taken from the app root, found as the nearest
parent folder that holds it; a relative one (``../Shared/_Nav``) from the view's folder. A name
built in code (``Layout = layoutName``) is not a quoted literal and gets no link (honest-null).
A Blazor ``@layout MainLayout`` names a class, not a file, and is not read here.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from ...schemas import FileRecord
from ..base import ParseContext

_VIEW_REFS = re.compile(
    rb"""\bLayout\s*=\s*"([^"]+)\""""
    rb"""|<partial\s[^>]*?\bname\s*=\s*"([^"]+)\""""
    rb"""|\bHtml\s*\.\s*(?:Render)?Partial(?:Async)?\s*\(\s*"([^"]+)\""""
)
#: Folders that end the upward search for a ``Shared`` folder (the view roots).
_VIEW_ROOTS = ("Pages", "Views")


def link_views(record: FileRecord, ctx: ParseContext) -> None:
    """Add the layout and partial views this view names to ``record.importFiles``."""
    root = Path(ctx.repo_root).resolve()
    view = PurePosixPath(ctx.path)
    for m in _VIEW_REFS.finditer(ctx.source):
        name = next(g for g in m.groups() if g is not None).decode("utf-8", "replace").strip()
        target = _find_view(name, view, root)
        if target is not None and target != ctx.path and target not in record.importFiles:
            record.importFiles.append(target)


def _find_view(name: str, view: PurePosixPath, root: Path) -> str | None:
    if not name or any(c in name for c in "@{}()"):
        return None  # an expression, not a view name
    ext = view.suffix
    if name.startswith(("~/", "/")):
        rel = PurePosixPath(name.lstrip("~/"))
        rel = rel if rel.suffix else rel.with_suffix(ext)
        return next((t for a in view.parent.parents if (t := _file(root, a / rel))), None)
    if "/" in name:
        rel = PurePosixPath(name)
        return _file(root, view.parent / (rel if rel.suffix else rel.with_suffix(ext)))
    file_name = name if name.endswith(ext) else name + ext
    dirs = _search_dirs(view)
    candidates = [view.parent / file_name] + [d / "Shared" / file_name for d in dirs]
    candidates += [a / sub / "Shared" / file_name for a in dirs[-1].parents for sub in _VIEW_ROOTS]
    return next((t for c in candidates if (t := _file(root, c)) is not None), None)


def _search_dirs(view: PurePosixPath) -> list[PurePosixPath]:
    """The view's folder and its parents, up to and including the ``Views`` / ``Pages`` root
    (or the repository root when there is none)."""
    dirs: list[PurePosixPath] = []
    for d in [view.parent, *view.parent.parents]:
        dirs.append(d)
        if d.name in _VIEW_ROOTS:
            break
    return dirs


def _file(root: Path, rel: PurePosixPath) -> str | None:
    target = (root / rel).resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return None
    return target.relative_to(root).as_posix()
