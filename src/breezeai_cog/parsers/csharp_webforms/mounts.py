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

# ``<%@ Register … Src="~/Controls/Nav.ascx" … %>``. TagPrefix/Namespace/Assembly Register
# variants carry no ``Src`` and are skipped (they register assembly controls, not a file).
# ``[^%]`` keeps the match inside a single directive (``%`` only starts the closing ``%>``).
_REGISTER_SRC = re.compile(
    rb"<%@\s*Register\b[^%]*?\bSrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
# ``LoadControl("~/Controls/Cart.ascx")`` — literal string arg only. ``LoadControl(var)`` /
# ``LoadControl(typeof(T))`` have no leading quote → unmatched (dynamic, unresolved).
_LOADCONTROL = re.compile(rb"LoadControl\s*\(\s*[\"']([^\"']+)[\"']")

_WEB_CONFIG = ("web.config", "Web.config")


def _app_root(rel_path: str, repo_root: Path) -> str:
    """Application root for ``~/`` resolution: the **shallowest** ancestor directory (repo
    root downward) holding a ``web.config``; falls back to the repo root (``""``)."""
    dirname = posixpath.dirname(rel_path)
    parts = dirname.split("/") if dirname else []
    for i in range(len(parts) + 1):
        d = "/".join(parts[:i])
        if any((repo_root / d / c).is_file() for c in _WEB_CONFIG):
            return d
    return ""


def _resolve(src: str, cur_dir: str, app_root: str, repo_root: Path) -> str | None:
    """A markup ``Src`` / ``LoadControl`` path → repo-relative ``.ascx.cs`` path, or None.

    ``~/``/``/`` resolve against the app root, a bare path against the host's own directory.
    Returns the control's code-behind path only when it exists on disk (else the ``IMPORTS``
    edge would dangle)."""
    s = src.strip().replace("\\", "/")
    if not s.lower().endswith(".ascx"):  # only user controls are mounts
        return None
    if s.startswith("~/"):
        rel = posixpath.normpath(posixpath.join(app_root, s[2:]))
    elif s.startswith("/"):
        rel = posixpath.normpath(posixpath.join(app_root, s[1:]))
    else:
        rel = posixpath.normpath(posixpath.join(cur_dir, s))
    if rel.startswith(".."):  # escaped the repo root
        return None
    cs = rel + ".cs"
    return cs if (repo_root / cs).is_file() else None


def resolve_mounts(
    abs_path: Path | None, rel_path: str, source: bytes, repo_root: Path | None
) -> list[str]:
    """Resolved control code-behind paths this page/control mounts — deduped, sorted for
    deterministic output. Needs on-disk access (``abs_path`` to read the sibling markup,
    ``repo_root`` to resolve/verify targets); returns ``[]`` when either is absent (e.g. an
    in-memory unit parse)."""
    if repo_root is None or abs_path is None:
        return []
    raw: list[bytes] = []
    # `<%@ Register Src %>` lives in the sibling markup (Page.aspx.cs → Page.aspx).
    text = str(abs_path)
    if text.endswith(".cs"):
        markup = Path(text[:-3])
        if markup.is_file():
            try:
                raw += _REGISTER_SRC.findall(markup.read_bytes())
            except OSError:
                pass
    # `LoadControl("…")` lives in the code-behind we already hold.
    raw += _LOADCONTROL.findall(source)

    app_root = _app_root(rel_path, repo_root)
    cur_dir = posixpath.dirname(rel_path)
    out: set[str] = set()
    for b in raw:
        resolved = _resolve(b.decode("utf-8", "replace"), cur_dir, app_root, repo_root)
        if resolved is not None:
            out.add(resolved)
    return sorted(out)
