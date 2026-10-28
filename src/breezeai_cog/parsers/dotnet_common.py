"""Helpers shared by the .NET framework parsers (``csharp_aspnet``, ``csharp_wcf``,
``csharp_hotchocolate``, ``csharp_webforms``, ``dotnet_servicehost``).

Two groups, both language-agnostic (they read names, type strings and repo paths, not AST
nodes) so C# and VB parsers share them verbatim:

* **attribute / type names** — :func:`simple_attr_name` (``[HttpGetAttribute]`` →
  ``HttpGet``) and :func:`response_dto` (``Task<ActionResult<List<Foo>>>`` → ``List<Foo>``);
* **IIS virtual paths** — :func:`web_app_root` (the ``web.config`` application root),
  :func:`to_repo_path` (``~/Controls/Nav.ascx`` → repo-relative) and :func:`ci_resolve`
  (case-insensitive lookup returning the real on-disk casing);
* **code-behind hosts** — :func:`code_behind_host` (the ``.asmx`` / ``.svc`` / ``.ashx`` file
  that serves a code-behind class).
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path


def simple_attr_name(name: str) -> str:
    """Normalize a C#/VB attribute name to its short form: an attribute may be written
    ``[HttpGet]`` or ``[HttpGetAttribute]`` (and svcutil emits the full form) — both bind to
    the same class, so drop a trailing ``Attribute``."""
    return name[: -len("Attribute")] if name.endswith("Attribute") and name != "Attribute" else name


def _has_top_level_comma(type_args: str) -> bool:
    """Whether ``type_args`` lists more than one type at its own nesting level —
    ``"string, int"`` yes, ``"Dictionary<string, int>"`` no."""
    depth = 0
    for ch in type_args:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "," and depth == 0:
            return True
    return False


#: Wrappers that are not part of the payload: async results and HTTP results (contract:
#: ``requestDTO`` / ``responseDTO`` are the declared payload type with these removed).
_NON_PAYLOAD_WRAPPERS = frozenset({
    "Task", "ValueTask", "ActionResult", "Ok", "Created", "CreatedAtRoute", "Accepted",
    "AcceptedAtRoute",
})
#: Declared types that carry no payload: no body, or a result that declares no type.
_NO_PAYLOAD = frozenset({
    "void", "Void", "Task", "ValueTask", "IActionResult", "ActionResult", "IResult",
    "IHttpActionResult", "HttpResponseMessage", "JsonResult", "ContentResult", "ViewResult",
    "PartialViewResult", "FileResult", "ObjectResult", "StatusCodeResult", "RedirectResult",
})
_VB_GENERIC_RE = re.compile(r"^([\w.]+)\(Of (.*)\)$")


def response_dto(return_type: str | None) -> str | None:
    """A handler's declared return type → the payload type as written, with async and
    HTTP-result wrappers removed (``Task<ActionResult<List<Foo>>>`` → ``List<Foo>``,
    ``Task(Of Foo)`` → ``Foo``); lists, arrays and nullable types stay as declared. None when
    no payload type is declared (``void``, ``Task``, ``IActionResult``)."""
    t = " ".join((return_type or "").split())
    while True:
        vb = _VB_GENERIC_RE.match(t)
        if "<" in t and t.endswith(">"):
            outer, inner = t[: t.index("<")], t[t.index("<") + 1: -1]
        elif vb is not None:
            outer, inner = vb.group(1), vb.group(2)
        else:
            break
        if outer.strip().rsplit(".", 1)[-1] not in _NON_PAYLOAD_WRAPPERS \
                or _has_top_level_comma(inner):
            break
        t = inner.strip()
    return None if not t or t.rsplit(".", 1)[-1] in _NO_PAYLOAD else t


def innermost_type(return_type: str | None) -> str | None:
    """The innermost single type argument, as a type name: ``Task<Session>`` → ``Session``
    (a generic with several arguments keeps its own name; a trailing ``?`` is dropped)."""
    if not return_type:
        return None
    t = return_type.strip()
    # C# generic: Foo<Bar> → Bar. A generic with several type arguments has no single payload
    # (`FieldResult<string, MyException>`, `Dictionary<string, int>`), so unwrapping stops there
    # and the declared outer type is kept — joining `Dictionary` to nothing is a clean miss,
    # whereas the concatenation `string, int` is not a type name at all.
    while "<" in t and t.endswith(">"):
        inner = t[t.index("<") + 1: -1].strip()
        if _has_top_level_comma(inner):
            t = t[: t.index("<")].strip()
            break
        t = inner
    # VB generic: Foo(Of Bar) → Bar
    while t.startswith(("Task(Of ", "ValueTask(Of ", "ActionResult(Of ")) and t.endswith(")"):
        t = t[t.index("(Of ") + 4: -1].strip()
    if t in ("IActionResult", "ActionResult", "void", "Void", "Task", "ValueTask", ""):
        return None
    # `Task<Track?>` unwraps to `Track?`; the nullable annotation is not part of the type name,
    # and keeping it means responseDTO names no captured class (a dead reference).
    return t.rstrip("?").strip() or None


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


_WEB_CONFIG = ("web.config", "Web.config")


def web_app_root(rel_path: str, repo_root: Path) -> str:
    """Application root for ``~/`` resolution: the **shallowest** ancestor directory (repo
    root downward) holding a ``web.config``; falls back to the repo root (``""``)."""
    dirname = posixpath.dirname(rel_path)
    parts = dirname.split("/") if dirname else []
    for i in range(len(parts) + 1):
        d = "/".join(parts[:i])
        if any((repo_root / d / c).is_file() for c in _WEB_CONFIG):
            return d
    return ""


def to_repo_path(src: str, cur_dir: str, app_root: str) -> str | None:
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


def code_behind_host(
    path: str, repo_root: Path | None, cls_name: str, ext: str, directive: re.Pattern[bytes]
) -> str | None:
    """The host file (``X.asmx`` / ``X.svc`` / ``X.ashx``) that serves class ``cls_name``
    declared in ``path`` — the code-behind convention ``X<ext>.cs`` beside ``X<ext>`` — when that
    file exists and its directive's class attribute names the class. Anything else → None (host
    unknown)."""
    if repo_root is None or not path.lower().endswith(ext + ".cs"):
        return None
    host = path[: -len(".cs")]
    try:
        body = (repo_root / host).read_bytes()
    except OSError:
        return None
    m = directive.search(body)
    simple = m.group(1).decode("utf-8", "replace").rsplit(".", 1)[-1] if m else None
    return host if simple == cls_name else None
