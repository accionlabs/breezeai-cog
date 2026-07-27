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

import os
import posixpath
import re
from pathlib import Path

#: per-process cache of ``abs-dir → {lowercased-name: real-name}`` for case-insensitive
#: resolution (repo is static during a run; each spawn worker builds its own). A value of
#: ``None`` marks a **case-only collision** — two entries with the same lowercased name (only
#: possible on a case-sensitive FS) — so the name resolves to honest-null, never a guess.
_CI_DIR_CACHE: dict[str, dict[str, str | None]] = {}


def _ci_listing(d: Path) -> dict[str, str | None]:
    key = str(d)
    cached = _CI_DIR_CACHE.get(key)
    if cached is None:
        cached = {}
        try:
            for e in os.scandir(d):
                low = e.name.lower()
                cached[low] = None if low in cached else e.name  # collision → ambiguous
        except OSError:
            cached = {}
        _CI_DIR_CACHE[key] = cached
    return cached


def ci_resolve(repo_root: Path, rel: str) -> str | None:
    """A repo-relative path → its **real on-disk casing** (matched case-insensitively), or
    ``None`` if absent — or **ambiguous**. Web Forms path refs are routinely mis-cased vs. the
    checked-out tree (they target a case-insensitive Windows/IIS filesystem), so we match
    ignoring case *and* return the actual path — the backend's join is case-sensitive, so the
    emitted path must carry the true casing to connect the nodes. If a directory holds two
    entries differing only in case, that name is ambiguous → ``None`` (honest-null, no guess)."""
    cur = repo_root
    real: list[str] = []
    for seg in rel.split("/"):
        if not seg or seg == ".":
            continue
        actual = _ci_listing(cur).get(seg.lower())  # None: missing OR case-only collision
        if actual is None:
            return None
        real.append(actual)
        cur = cur / actual
    return "/".join(real) if real else None

# ``<%@ Register … Src="~/Controls/Nav.ascx" … %>``. TagPrefix/Namespace/Assembly Register
# variants carry no ``Src`` and are skipped (they register assembly controls, not a file).
# ``[^%]`` keeps the match inside a single directive (``%`` only starts the closing ``%>``).
_REGISTER_SRC = re.compile(
    rb"<%@\s*Register\b[^%]*?\bSrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
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

_WEB_CONFIG = ("web.config", "Web.config")


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


def _to_repo_path(src: str, cur_dir: str, app_root: str) -> str | None:
    """A markup virtual path → normalized repo-relative path. ``~/``/``/`` resolve against
    the app root, a bare path against the host's own directory; ``None`` if it escapes the
    repo root."""
    s = src.strip().replace("\\", "/")
    if s.startswith("~/"):
        rel = posixpath.normpath(posixpath.join(app_root, s[2:]))
    elif s.startswith("/"):
        rel = posixpath.normpath(posixpath.join(app_root, s[1:]))
    else:
        rel = posixpath.normpath(posixpath.join(cur_dir, s))
    return None if rel.startswith("..") else rel


def _resolve_mount(src: str, cur_dir: str, app_root: str, repo_root: Path) -> str | None:
    """A ``Src`` / ``LoadControl`` path → the control's repo-relative ``.ascx.cs`` path in its
    real on-disk casing, only when it exists (else the ``IMPORTS`` edge would dangle)."""
    rel = _to_repo_path(src, cur_dir, app_root)
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
    app_root = _app_root(rel_path, repo_root)
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
    rel = _to_repo_path(
        m.group(1).decode("utf-8", "replace"), posixpath.dirname(rel_path),
        _app_root(rel_path, repo_root),
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
