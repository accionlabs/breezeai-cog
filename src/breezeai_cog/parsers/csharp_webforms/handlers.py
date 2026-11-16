"""ASP.NET generic handlers (``.ashx``): an ``IHttpHandler`` class served at its ``.ashx`` URL.

``Export.ashx`` (``<%@ WebHandler Class="Export" CodeBehind="Export.ashx.cs" %>``) serves the
class in ``Export.ashx.cs``; IIS calls its ``ProcessRequest`` (``ProcessRequestAsync`` for an
``HttpTaskAsyncHandler``) for every request to the ``.ashx`` path. That method is the binding:
``route``, ``ANY`` (the handler answers every verb), ``endpoint`` = ``/<path>.ashx`` — the same
convention as a Web Forms page — and ``handler`` / ``handlerLine`` = the method.

**Additive**: a handler's code-behind usually imports only ``System.Web``, so no framework parser
claims it; this runs for every C# file, like the Lambda detector. The class is linked to its
``.ashx`` by the code-behind convention (``X.ashx.cs`` beside ``X.ashx`` whose ``Class=`` names
it). A class ``web.config`` maps a path to (:mod:`.config_handlers`) gets one record per
``(endpoint, method)`` it is registered with, on the same method. A handler class with neither is
not reachable by a known URL, so it gets no record.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from ...schemas import FileRecord
from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..dotnet_common import code_behind_host
from ..emits import Emit
from .config_handlers import config_handlers

_WEBHANDLER_CLASS_RE = re.compile(
    rb'<%@\s*WebHandler\b[^%]*?\bClass\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE
)
#: Bases that make a class a generic handler, and the method IIS calls on each.
_HANDLER_BASES = {
    "IHttpHandler": "ProcessRequest",
    "IHttpAsyncHandler": "BeginProcessRequest",
    "HttpTaskAsyncHandler": "ProcessRequestAsync",
}
#: The method the runtime calls on a registered handler, by what the class declares: the base
#: may be the repo's own class, so it is not required. A factory hands each request on.
_ENTRY_METHODS = ("ProcessRequestAsync", "ProcessRequest", "BeginProcessRequest", "GetHandler")


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


def _class_fqn(cls_name: str, path: str, index: object) -> str | None:
    """The fully-qualified name of the one type named ``cls_name`` declared in ``path``."""
    types = getattr(index, "types", None)
    if not isinstance(types, dict):
        return None
    fqns = [fqn for fqn, files in types.items()
            if fqn.rsplit(".", 1)[-1] == cls_name and path in files]
    return fqns[0] if len(fqns) == 1 else None


def detect_registered_handlers(
    anchor: Node, path: str, record: FileRecord, index: object
) -> list[Emit]:
    """One ``route`` per ``(endpoint, method)`` a ``web.config`` registers a class of this file
    under. A type declared over several files gets its records in the file declaring its entry
    method, so they are not repeated."""
    registered = config_handlers(index).by_fqn
    if not registered:
        return []
    types = getattr(index, "types", None)
    names = {fqn.rsplit(".", 1)[-1] for fqn in registered}
    found: list[Emit] = []
    for cls in record.classes:
        fqn = _class_fqn(cls.name, path, index) if cls.name in names else None
        pairs = registered.get(fqn) if fqn is not None else None
        if not pairs:
            continue
        methods = {f.name: f for f in record.functions if f.parentId == cls.id}
        method = next((methods[n] for n in _ENTRY_METHODS if n in methods), None)
        if method is None and len(types.get(fqn, ())) > 1:  # type: ignore[union-attr]
            continue
        for endpoint, verb in pairs:
            found.append(Emit(
                anchor,
                {
                    "semanticType": "route",
                    "method": verb,
                    "endpoint": endpoint,
                    "framework": "aspnet-webforms",
                    "routeKind": "route",
                    "handler": method.name if method is not None else None,
                    "handlerLine": method.startLine if method is not None else None,
                },
                parent=method.id if method is not None else cls.id, node_type="synthetic",
                text=endpoint, line=method.startLine if method is not None else cls.startLine,
                col=0, end_line=method.endLine if method is not None else cls.endLine,
            ))
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found: list[Emit] = []
    if dc.path.lower().endswith(".ashx.cs") and b"Handler" in dc.source:
        found += detect_generic_handlers(dc.root, dc.path, dc.record, dc.ctx.repo_root)
    found += detect_registered_handlers(dc.root, dc.path, dc.record, dc.ctx.resolution_index)
    return Detection(found, label="aspnet-webforms") if found else None


# An entry-point emitter, so fixture/test files are skipped. No byte guard: a registered class
# may name no handler type at all (its base is the repo's own); _detect checks are a suffix and
# byte check and a dictionary lookup.
register_detector(Detector(
    name="generic-handlers", gate=Gate("csharp", order=40, skip_fixtures=True),
    detect=_detect, frameworks=("aspnet-webforms",),
))
