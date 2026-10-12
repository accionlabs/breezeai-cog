"""WCF service-contract detection (off the record, like the ASP.NET controller detector).

A WCF service is a ``[ServiceContract]``-decorated interface; each ``[OperationContract]``
method is a callable operation (an entry point over SOAP/net.tcp, or REST when the method
also carries ``[WebGet]``/``[WebInvoke]``). The C# base parser already captured these
attributes onto ``Class.decorators`` / ``Function.decorators``, so this detector just reads
the ``FileRecord`` — no AST re-walk, no new grammar.

Emitted as ``route`` statements:
* ``semanticType=route``, ``framework=wcf``, ``handler`` = the operation method.
* ``endpoint`` = ``ServiceName/OperationName`` (SOAP/RPC) or the ``UriTemplate`` (REST-over-WCF).
* ``method`` = ``RPC`` for SOAP operations; the HTTP verb for ``[WebGet]``/``[WebInvoke]``.
* ``routeKind`` = ``rpc`` (SOAP) or ``route`` (REST-over-WCF).
"""

from __future__ import annotations

import re
from pathlib import Path

from ...emit import disambiguate, statement_id
from ...schemas import Decorator, FileRecord, Statement
from ..dotnet_common import response_dto, simple_attr_name

_SERVICE_ATTR = "ServiceContract"
_OP_ATTR = "OperationContract"
_WEB_GET = "WebGet"
_WEB_INVOKE = "WebInvoke"
_WEB_SERVICE_ATTR = "WebService"   # ASMX (System.Web.Services)
_WEB_METHOD_ATTR = "WebMethod"     # ASMX operation
_GENERATED_ATTR = "GeneratedCode"  # svcutil-generated client proxy — NOT a server entry point
_HTTP_VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
#: ``Class="Ns.Type"`` inside an ``.asmx`` ``<%@ WebService … %>`` directive.
_ASMX_CLASS_RE = re.compile(rb'<%@\s*WebService\b[^%]*?\bClass\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


def _has_attr(decorators: list[Decorator], name: str) -> bool:
    """Whether a decorator with the given (short) attribute name is present — tolerant of
    the full ``…Attribute`` form (e.g. ``[ServiceContractAttribute]``)."""
    return any(simple_attr_name(d.name) == name for d in decorators)

# NOTE: the C# attribute extractor reduces a string-literal named arg (``Name = "X"``) to its
# bare value ("X") — the key is dropped (see csharp/functions.py). So named args cannot be
# looked up by key; we recover them by the *shape* of the value instead.


def _uri_arg(dec: Decorator) -> str | None:
    """The ``UriTemplate`` value of a WebGet/WebInvoke — the arg shaped like a path."""
    for a in dec.args:
        s = a.strip()
        if s.startswith("/") or "{" in s:
            return s
    return None


def _verb_arg(dec: Decorator) -> str | None:
    """The ``Method = "…"`` value of a WebInvoke — the arg that is an HTTP verb."""
    for a in dec.args:
        s = a.strip().upper()
        if s in _HTTP_VERBS:
            return s
    return None


def _name_arg(decorators: list[Decorator], attr: str) -> str | None:
    """The bare-identifier ``Name="X"`` value on the given attribute, if present (a plain
    identifier — not a namespace URI)."""
    for d in decorators:
        if simple_attr_name(d.name) == attr:
            for a in d.args:
                s = a.strip()
                if s and not any(c in s for c in "=/.:"):
                    return s
    return None


def _service_name(decorators: list[Decorator], class_name: str) -> str:
    """The WCF service name: the ``[ServiceContract(Name="X")]`` value when present, else the
    interface name with a conventional leading ``I`` dropped (``IEnrollmentService`` →
    ``EnrollmentService``)."""
    name = _name_arg(decorators, _SERVICE_ATTR)
    if name:
        return name
    if len(class_name) > 1 and class_name[0] == "I" and class_name[1].isupper():
        return class_name[1:]
    return class_name


def detect_wcf_services(record: FileRecord) -> list[Statement]:
    """One ``route`` statement per ``[OperationContract]`` under a ``[ServiceContract]`` type
    (WCF / CoreWCF; tolerant of the full ``…Attribute`` form)."""
    services: dict[str, str] = {
        cls.id: _service_name(cls.decorators, cls.name)
        for cls in record.classes
        if _has_attr(cls.decorators, _SERVICE_ATTR) and not _has_attr(cls.decorators, _GENERATED_ATTR)
    }
    if not services:
        return []

    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
    for fn in record.functions:
        svc = services.get(fn.parentId)
        if svc is None or not _has_attr(fn.decorators, _OP_ATTR):
            continue
        web = next((d for d in fn.decorators if simple_attr_name(d.name) in (_WEB_GET, _WEB_INVOKE)), None)
        if web is not None:  # REST-over-WCF (WebHttpBinding)
            method = "GET" if simple_attr_name(web.name) == _WEB_GET else (_verb_arg(web) or "POST")
            endpoint = _uri_arg(web) or f"{svc}/{fn.name}"
            route_kind = "route"
        else:  # SOAP / net.tcp RPC
            method, endpoint, route_kind = "RPC", f"{svc}/{fn.name}", "rpc"
        routes.append(
            Statement(
                id=disambiguate(statement_id(fn.path, fn.startLine, 0), seen),
                parentId=fn.id,
                nodeType="synthetic",
                semanticType="route",
                text=f"[{_OP_ATTR}] {svc}.{fn.name}",
                method=method,
                endpoint=endpoint,
                framework="wcf",
                handler=fn.name,
                handlerLine=fn.startLine,
                routeKind=route_kind,
                isRegex=False,
                responseDTO=response_dto(fn.returnType),
                startLine=fn.startLine,
                endLine=fn.endLine,
                path=fn.path,
            )
        )
    return routes


def asmx_host(path: str, repo_root: Path | None, cls_name: str) -> str | None:
    """The ``.asmx`` file that serves class ``cls_name`` declared in ``path`` — the code-behind
    convention ``X.asmx.cs`` beside ``X.asmx`` — when that file exists and its ``WebService``
    directive's ``Class=`` names the class. Anything else → None (the host is not known)."""
    if repo_root is None or not path.lower().endswith(".asmx.cs"):
        return None
    host = path[: -len(".cs")]
    try:
        body = (repo_root / host).read_bytes()
    except OSError:
        return None
    m = _ASMX_CLASS_RE.search(body)
    simple = m.group(1).decode("utf-8", "replace").rsplit(".", 1)[-1] if m else None
    return host if simple == cls_name else None


def detect_asmx_services(record: FileRecord, repo_root: Path | None = None) -> list[Statement]:
    """Legacy ASMX SOAP web services (``System.Web.Services``): each ``[WebMethod]`` method is
    an operation. ``[WebService]`` on the class is optional in ASMX, so the ``[WebMethod]``
    methods themselves drive detection. Emitted like WCF SOAP ops (``framework=asmx``, ``rpc``).
    ``endpoint`` is the served address ``<.asmx path>/<Method>`` (the ``.asmx`` path is
    repo-relative, as on the host record) when the ``.asmx`` file is found
    (:func:`asmx_host`); otherwise empty — a service or class name is not an address."""
    cls_by_id = {c.id: c for c in record.classes}
    seen = {s.id for s in record.statements}
    routes: list[Statement] = []
    for fn in record.functions:
        if not _has_attr(fn.decorators, _WEB_METHOD_ATTR):
            continue
        cls = cls_by_id.get(fn.parentId)
        if cls is not None and _has_attr(cls.decorators, _GENERATED_ATTR):
            continue  # generated proxy, not a real ASMX service
        svc = (_name_arg(cls.decorators, _WEB_SERVICE_ATTR) or cls.name) if cls is not None else "WebService"
        host = asmx_host(fn.path or record.path, repo_root, cls.name) if cls is not None else None
        routes.append(
            Statement(
                id=disambiguate(statement_id(fn.path, fn.startLine, 0), seen),
                parentId=fn.id,
                nodeType="synthetic",
                semanticType="route",
                text=f"[{_WEB_METHOD_ATTR}] {svc}.{fn.name}",
                method="RPC",
                endpoint=f"{host}/{fn.name}" if host is not None else None,
                framework="asmx",
                handler=fn.name,
                handlerLine=fn.startLine,
                routeKind="rpc",
                isRegex=False,
                responseDTO=response_dto(fn.returnType),
                startLine=fn.startLine,
                endLine=fn.endLine,
                path=fn.path,
            )
        )
    return routes
