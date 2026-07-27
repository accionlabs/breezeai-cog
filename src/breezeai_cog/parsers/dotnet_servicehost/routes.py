"""ServiceHost-style directive parsing for .NET endpoint-host markup → one ``route`` statement.

Two file types share the same shape — a single ``<%@ … %>`` directive naming the **concrete
class** hosted at an endpoint, plus a ``CodeBehind`` pointing at its ``.cs``:

* ``.svc``  (WCF) — ``<%@ ServiceHost Service="Acme.Services.OrderService" CodeBehind="…" %>``
* ``.asmx`` (ASMX SOAP) — ``<%@ WebService Class="Acme.Services.OrderService" CodeBehind="…" %>``

The code-behind (``.svc.cs``/``.asmx.cs``) is ordinary C# (parsed elsewhere); the markup file
has no C# body, so this is a lightweight directive scan (no tree-sitter), mirroring the Web
Forms ``<%@ Register %>`` pass. The load-bearing value is the class FQN (``Service=``/
``Class=``): it resolves the interface→concrete-impl ambiguity that ``.cs`` alone cannot (an
interface may have several implementers; only the host says which one is served / which class
backs the endpoint URL).

Both emit a ``route`` reusing existing vocabulary (``routeKind="rpc"``, ``method="RPC"``; the
``framework`` differs — ``wcf`` vs ``asmx`` — both already valid enum values). The per-type
differences (directive keyword, FQN attribute name, framework label) are **data** in
:data:`DIRECTIVES`, not branches. Attributes may appear in any order, span lines, and vary in
case.

Honest-null throughout: no directive / no class attribute → no statement; a ``CodeBehind``
that does not resolve on disk → no import edge (never a dangling one).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..csharp_webforms.mounts import ci_resolve


@dataclass(frozen=True)
class _HostSpec:
    """A ServiceHost-style directive type: the directive keyword, the attribute naming the
    concrete class, the emitted framework label, and the file's language tag."""

    directive: str  # ServiceHost / WebService
    class_attr: str  # Service / Class
    framework: str  # wcf / asmx
    language: str  # svc / asmx


#: Extension → directive spec. Adding another ServiceHost-style file type is one row here.
DIRECTIVES: dict[str, _HostSpec] = {
    ".svc": _HostSpec("ServiceHost", "Service", "wcf", "svc"),
    ".asmx": _HostSpec("WebService", "Class", "asmx", "asmx"),
}


def spec_for(path: str) -> _HostSpec | None:
    """The directive spec for a path's extension (``.svc``/``.asmx``), else None."""
    return DIRECTIVES.get(posixpath.splitext(path)[1].lower())


def _directive_re(keyword: str) -> re.Pattern[bytes]:
    # ``[^%]`` keeps the match inside one ``<%@ … %>`` (``%`` only begins the closing ``%>``)
    # and spans newlines (``\n`` is not ``%``). Case-insensitive.
    return re.compile(rb"<%@\s*" + keyword.encode() + rb"\b([^%]*)%>", re.IGNORECASE)


def _attr(body: bytes, name: str) -> str | None:
    """The value of ``name="…"`` within a directive body (any order/case), else None."""
    m = re.search(
        rb"\b" + re.escape(name.encode()) + rb'\s*=\s*["\']([^"\']+)["\']', body, re.IGNORECASE
    )
    return m.group(1).decode("utf-8", "replace").strip() if m else None


def _codebehind_import(code_behind: str, rel_path: str, repo_root: Path | None) -> str | None:
    """The ``CodeBehind`` attribute → the code-behind ``.cs`` repo path (real on-disk casing)
    for the IMPORTS edge, or None when absent / unresolved (honest-null — no dangling edge).
    Resolved relative to the host file's own directory (the standard CodeBehind convention)."""
    if repo_root is None:
        return None
    rel = posixpath.normpath(
        posixpath.join(posixpath.dirname(rel_path), code_behind.replace("\\", "/"))
    )
    if rel.startswith(".."):  # escaped the repo root
        return None
    return ci_resolve(repo_root, rel)


def detect_service_host(
    source: bytes, rel_path: str, repo_root: Path | None, *, seen_ids: set[str]
) -> tuple[Statement | None, list[str]]:
    """Parse the ServiceHost/WebService directive of a ``.svc``/``.asmx`` file. Returns
    ``(route_statement_or_None, import_paths)``. Emits nothing when the path is neither type,
    the file has no directive, or the directive names no concrete class (honest-null)."""
    spec = spec_for(rel_path)
    if spec is None:
        return None, []
    m = _directive_re(spec.directive).search(source)
    if m is None:
        return None, []
    body = m.group(1)
    cls = _attr(body, spec.class_attr)
    if not cls:  # a directive with no concrete class → nothing to bind (honest-null)
        return None, []

    imports: list[str] = []
    code_behind = _attr(body, "CodeBehind")
    if code_behind:
        resolved = _codebehind_import(code_behind, rel_path, repo_root)
        if resolved is not None:
            imports.append(resolved)

    line = source[: m.start()].count(b"\n") + 1  # 1-based line of the directive
    col = m.start() - (source.rfind(b"\n", 0, m.start()) + 1)
    stmt = Statement(
        id=disambiguate(statement_id(rel_path, line, col), seen_ids),
        parentId=file_id(rel_path),  # the host file owns the endpoint (no functions here)
        nodeType="synthetic",  # directive-derived, no backing AST node
        semanticType="route",
        text=f"{spec.directive} {cls}",
        framework=spec.framework,
        method="RPC",  # SOAP operation host — addressed by name, no HTTP verb
        endpoint=rel_path,  # the served endpoint (physical path)
        handler=cls,  # the concrete impl FQN — resolves interface/URL → concrete class
        routeKind="rpc",
        isRegex=False,
        startLine=line,
        endLine=source[: m.end()].count(b"\n") + 1,
        path=rel_path,
    )
    return stmt, imports
