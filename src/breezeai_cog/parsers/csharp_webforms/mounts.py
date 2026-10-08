"""Web Forms host→control mount resolution (markup pass, item 1).

A page/control declares the user controls it composes in two places:

* **markup** — ``<%@ Register Src="~/Controls/Nav.ascx" %>`` (in the sibling ``.aspx``/
  ``.ascx``/``.master``, which is not itself scanned/parsed);
* **code-behind** — ``LoadControl("~/Controls/Cart.ascx")`` (a literal-arg call).

Each is resolved to the control's **code-behind** path (``…/Nav.ascx.cs`` — the file that
actually has a ``FileRecord``; markup files get none) and added to the host's
``importFiles``, building the ``IMPORTS`` edge host→control. This reuses the existing
load-bearing field — no new schema field or type.

Honest-null throughout: **dynamic** ``LoadControl(var)`` (no string literal), a control with
no code-behind, or a path that escapes the repo resolve to nothing — a missing edge is
always preferred to a wrong one (spec §3.1: the backend silently skips a dangling edge)."""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from ..dotnet_common import ci_resolve, to_repo_path, web_app_root

# ``<%@ Register … Src="~/Controls/Nav.ascx" … %>``. TagPrefix/Namespace/Assembly Register
# variants carry no ``Src`` and are skipped (they register assembly controls, not a file).
# ``[^%]`` keeps the match inside a single directive (``%`` only starts the closing ``%>``).
_REGISTER_SRC = re.compile(rb"<%@\s*Register\b[^%]*?\bSrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
# ``LoadControl("~/Controls/Cart.ascx")`` — literal string arg only. ``LoadControl(var)`` /
# ``LoadControl(typeof(T))`` have no leading quote → unmatched (dynamic, unresolved).
_LOADCONTROL = re.compile(rb"LoadControl\s*\(\s*[\"']([^\"']+)[\"']")
# ``<%@ Page MasterPageFile="~/Site.master" %>`` / ``<%@ Master MasterPageFile=… %>`` — a
# page/control/master's layout parent. Directive-anchored (``[^%]`` stays inside the ``<%@…%>``)
# so it never matches ``MasterPageFile`` in an HTML comment or attribute.
_MASTER_FILE = re.compile(
    rb"<%@\s*(?:Page|Master|Control)\b[^%]*?\bMasterPageFile\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

def read_sibling_markup(abs_path: Path | None) -> bytes:
    """The page/control/master markup bytes beside a code-behind (``Page.aspx.cs`` →
    ``Page.aspx``); ``b""`` when there is no on-disk path or no sibling. Read once and shared
    by the mount + master passes."""
    if abs_path is None:
        return b""
    text = str(abs_path)
    if not text.endswith(".cs"):
        return b""
    markup = Path(text[:-3])
    if not markup.is_file():
        return b""
    try:
        return markup.read_bytes()
    except OSError:
        return b""


def _resolve_mount(src: str, cur_dir: str, app_root: str, repo_root: Path) -> str | None:
    """A ``Src`` / ``LoadControl`` path → the control's repo-relative ``.ascx.cs`` path in its
    real on-disk casing, only when it exists (else the ``IMPORTS`` edge would dangle)."""
    rel = to_repo_path(src, cur_dir, app_root)
    if rel is None or not rel.lower().endswith(".ascx"):  # only user controls are mounts
        return None
    return ci_resolve(repo_root, rel + ".cs")


def resolve_mounts(
    markup: bytes, rel_path: str, source: bytes, repo_root: Path | None
) -> list[str]:
    """Resolved control code-behind paths this page/control mounts — deduped, sorted for
    deterministic output. ``markup`` is the sibling markup bytes (``<%@ Register Src %>``),
    ``source`` the code-behind (``LoadControl("…")``). Returns ``[]`` when ``repo_root`` is
    absent (in-memory unit parse) — targets can't be verified."""
    if repo_root is None:
        return []
    raw = _REGISTER_SRC.findall(markup) + _LOADCONTROL.findall(source)
    app_root = web_app_root(rel_path, repo_root)
    cur_dir = posixpath.dirname(rel_path)
    out: set[str] = set()
    for b in raw:
        resolved = _resolve_mount(b.decode("utf-8", "replace"), cur_dir, app_root, repo_root)
        if resolved is not None:
            out.add(resolved)
    return sorted(out)


def resolve_master(markup: bytes, rel_path: str, repo_root: Path | None) -> str | None:
    """The layout endpoint this page/control/master composes into — the ``MasterPageFile``
    directive resolved to a repo-relative ``/…​.master`` path — or ``None``. Emitted only for a
    literal directive whose ``.master`` target exists on disk (honest-null); returns ``None``
    without ``repo_root`` (in-memory parse)."""
    if repo_root is None:
        return None
    m = _MASTER_FILE.search(markup)
    if m is None:
        return None
    rel = to_repo_path(
        m.group(1).decode("utf-8", "replace"),
        posixpath.dirname(rel_path),
        web_app_root(rel_path, repo_root),
    )
    if rel is None or not rel.lower().endswith(".master"):
        return None
    actual = ci_resolve(repo_root, rel)  # case-insensitive → real on-disk casing
    return "/" + actual if actual is not None else None


def master_codebehind(master_endpoint: str | None, repo_root: Path | None) -> str | None:
    """The master's **code-behind** repo path (for the page→master ``IMPORTS`` edge), in real
    on-disk casing, or ``None`` when there's no master or it has no code-behind (an inline-code
    master → honest-null, no dangling edge). ``master_endpoint`` is the already-resolved
    ``/…​.master`` from :func:`resolve_master`; its ``.cs`` sibling is the master's FileRecord."""
    if master_endpoint is None or repo_root is None:
        return None
    return ci_resolve(repo_root, master_endpoint.lstrip("/") + ".cs")
