"""ASP.NET generic handlers (``.ashx``): an ``IHttpHandler`` class served at its ``.ashx`` URL.

``Export.ashx`` (``<%@ WebHandler Class="Export" CodeBehind="Export.ashx.cs" %>``) serves the
class in ``Export.ashx.cs``; IIS calls its ``ProcessRequest`` (``ProcessRequestAsync`` for an
``HttpTaskAsyncHandler``) for every request to the ``.ashx`` path. That method is the binding:
``route``, ``ANY`` (the handler answers every verb), ``endpoint`` = ``/<path>.ashx`` — the same
convention as a Web Forms page — and ``handler`` / ``handlerLine`` = the method.

**Additive**: a handler's code-behind usually imports only ``System.Web``, so no framework parser
claims it; this runs for every C# file, like the Lambda detector. The class is linked to its
``.ashx`` by the code-behind convention (``X.ashx.cs`` beside ``X.ashx`` whose ``Class=`` names
it); a handler class without such a file is not reachable by a known URL, so it gets no record.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Statement
from ..additive import DetectContext, Detector, Gate, register_detector
from ..dotnet_common import code_behind_host

_WEBHANDLER_CLASS_RE = re.compile(
    rb'<%@\s*WebHandler\b[^%]*?\bClass\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE
)
#: Bases that make a class a generic handler, and the method IIS calls on each.
_HANDLER_BASES = {
    "IHttpHandler": "ProcessRequest",
    "IHttpAsyncHandler": "BeginProcessRequest",
    "HttpTaskAsyncHandler": "ProcessRequestAsync",
}


def detect_generic_handlers(path: str, record: FileRecord, repo_root: object) -> bool:
    """Add one ``route`` per generic-handler class whose ``.ashx`` is found; True if any."""
    root = repo_root if isinstance(repo_root, Path) else None
    seen = {s.id for s in record.statements}
    found = False
    for cls in record.classes:
        bases = [b.split("<", 1)[0].rsplit(".", 1)[-1] for b in [cls.extends or "", *cls.implements]]
        entry = next((_HANDLER_BASES[b] for b in bases if b in _HANDLER_BASES), None)
        host = code_behind_host(path, root, cls.name, ".ashx", _WEBHANDLER_CLASS_RE) \
            if entry is not None else None
        if entry is None or host is None:
            continue
        method = next((f for f in record.functions
                       if f.parentId == cls.id and f.name == entry), None)
        line = method.startLine if method is not None else cls.startLine
        new_id = disambiguate(statement_id(path, line, 0), seen)
        seen.add(new_id)
        record.statements.append(Statement(
            id=new_id,
            parentId=method.id if method is not None else cls.id,
            nodeType="synthetic",
            semanticType="route",
            text=host.rsplit("/", 1)[-1],
            method="ANY",
            endpoint="/" + host.lstrip("/"),
            framework="aspnet-webforms",
            routeKind="route",
            handler=entry if method is not None else None,
            handlerLine=method.startLine if method is not None else None,
            startLine=line,
            endLine=method.endLine if method is not None else cls.endLine,
            path=path,
        ))
        found = True
    return found


def _run(dc: DetectContext) -> str | None:
    if not dc.path.lower().endswith(".ashx.cs"):
        return None
    return "aspnet-webforms" if detect_generic_handlers(dc.path, dc.record, dc.ctx.repo_root) \
        else None


# An entry-point emitter, so fixture/test files are skipped.
register_detector(Detector(
    name="generic-handlers", gate=Gate("csharp", order=40, guard=b"Handler", skip_fixtures=True),
    run=_run, frameworks=("aspnet-webforms",),
))
