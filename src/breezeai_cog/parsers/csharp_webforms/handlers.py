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

from tree_sitter import Node

from ...schemas import FileRecord
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..dotnet_common import code_behind_host
from ..emits import Emit

_WEBHANDLER_CLASS_RE = re.compile(
    rb'<%@\s*WebHandler\b[^%]*?\bClass\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE
)
#: Bases that make a class a generic handler, and the method IIS calls on each.
_HANDLER_BASES = {
    "IHttpHandler": "ProcessRequest",
    "IHttpAsyncHandler": "BeginProcessRequest",
    "HttpTaskAsyncHandler": "ProcessRequestAsync",
}


def detect_generic_handlers(
    anchor: Node, path: str, record: FileRecord, repo_root: object
) -> list[Emit]:
    """One ``route`` per generic-handler class whose ``.ashx`` is found. The record is built from
    the class and method records, so ``anchor`` (the file's root) only carries the emit; every
    span field is given explicitly."""
    root = repo_root if isinstance(repo_root, Path) else None
    found: list[Emit] = []
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
        found.append(Emit(
            anchor,
            {
                "semanticType": "route",
                "method": "ANY",
                "endpoint": "/" + host.lstrip("/"),
                "framework": "aspnet-webforms",
                "routeKind": "route",
                "handler": entry if method is not None else None,
                "handlerLine": method.startLine if method is not None else None,
            },
            parent=method.id if method is not None else cls.id, node_type="synthetic",
            text=host.rsplit("/", 1)[-1], line=line, col=0,
            end_line=method.endLine if method is not None else cls.endLine,
        ))
    return found


def _detect(dc: DetectContext) -> Detection | None:
    if not dc.path.lower().endswith(".ashx.cs"):
        return None
    found = detect_generic_handlers(dc.root, dc.path, dc.record, dc.ctx.repo_root)
    return Detection(found, label="aspnet-webforms") if found else None


# An entry-point emitter, so fixture/test files are skipped.
register_detector(Detector(
    name="generic-handlers", gate=Gate("csharp", order=40, guard=b"Handler", skip_fixtures=True),
    detect=_detect, frameworks=("aspnet-webforms",),
))
