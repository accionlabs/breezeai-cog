"""ServiceHost directive parsing for a WCF ``.svc`` file → one ``route`` statement.

A ``.svc`` carries a single directive naming the concrete service class hosted at the
endpoint::

    <%@ ServiceHost Language="C#" Service="KUCare.Services.AttendanceService"
        Factory="System.ServiceModel.Activation.ServiceHostFactory"
        CodeBehind="AttendanceService.svc.cs" %>

Attributes may appear in any order, span multiple lines, and vary in case. This is markup,
not C#, so detection is a directive scan (no tree-sitter parse) — mirroring the Web Forms
``<%@ Register %>`` markup pass. Emits a ``route`` statement reusing the existing WCF
vocabulary (``framework="wcf"``, ``routeKind="rpc"``, ``method="RPC"`` — no new enum value):

* ``endpoint``  = the ``.svc`` path (the served endpoint),
* ``handler``   = the ``Service`` FQN (the **concrete implementation class** — the value that
  resolves the ``[ServiceContract]`` interface → concrete-class ambiguity a ``.cs`` alone
  cannot; an interface may have several implementers, only the host says which is served),
* ``importFiles`` (on the FileRecord) = the resolved ``CodeBehind`` ``.cs`` path (IMPORTS edge).

Honest-null throughout: no directive / no ``Service`` → no statement emitted; a ``CodeBehind``
that does not resolve on disk → no import edge (never a dangling one).
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..csharp_webforms.mounts import ci_resolve

# The ServiceHost directive; ``[^%]`` keeps the match inside one ``<%@ … %>`` (``%`` only
# begins the closing ``%>``) and spans newlines (``\n`` is not ``%``). Case-insensitive.
_DIRECTIVE = re.compile(rb"<%@\s*ServiceHost\b([^%]*)%>", re.IGNORECASE)


def _attr(body: bytes, name: str) -> str | None:
    """The value of ``name="…"`` within a directive body (any order/case), else None."""
    m = re.search(
        rb"\b" + re.escape(name.encode()) + rb'\s*=\s*["\']([^"\']+)["\']', body, re.IGNORECASE
    )
    return m.group(1).decode("utf-8", "replace").strip() if m else None


def _codebehind_import(code_behind: str, rel_path: str, repo_root: Path | None) -> str | None:
    """The ``CodeBehind`` attribute → the ``.svc.cs`` repo path (in real on-disk casing) for the
    IMPORTS edge, or None when absent / unresolved (honest-null — no dangling edge). The path is
    relative to the ``.svc``'s own directory (the standard CodeBehind convention)."""
    if repo_root is None:
        return None
    rel = posixpath.normpath(
        posixpath.join(posixpath.dirname(rel_path), code_behind.replace("\\", "/"))
    )
    if rel.startswith(".."):  # escaped the repo root
        return None
    return ci_resolve(repo_root, rel)


def detect_svc_host(
    source: bytes, rel_path: str, repo_root: Path | None, *, seen_ids: set[str]
) -> tuple[Statement | None, list[str]]:
    """Parse the ServiceHost directive. Returns ``(route_statement_or_None, import_paths)``.
    Emits nothing when the file has no directive or the directive names no ``Service``."""
    m = _DIRECTIVE.search(source)
    if m is None:
        return None, []
    body = m.group(1)
    service = _attr(body, "Service")
    if not service:  # a ServiceHost with no concrete class → nothing to bind (honest-null)
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
        parentId=file_id(rel_path),  # the .svc FileRecord owns the endpoint (no functions here)
        nodeType="synthetic",  # directive-derived, no backing AST node
        semanticType="route",
        text=f"ServiceHost {service}",
        framework="wcf",
        method="RPC",  # SOAP operation host — addressed by name, no HTTP verb
        endpoint=rel_path,  # the served .svc endpoint (physical path)
        handler=service,  # the concrete impl FQN — resolves interface → impl
        routeKind="rpc",
        isRegex=False,
        startLine=line,
        endLine=source[: m.end()].count(b"\n") + 1,
        path=rel_path,
    )
    return stmt, imports
