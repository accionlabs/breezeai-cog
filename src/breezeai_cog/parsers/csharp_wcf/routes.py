"""WCF service-contract detection (off the record, like the ASP.NET controller detector).

A WCF service is a ``[ServiceContract]``-decorated interface; each ``[OperationContract]``
method is a callable operation (an entry point over SOAP/net.tcp, or REST when the method
also carries ``[WebGet]``/``[WebInvoke]``). The C# base parser already captured these
attributes onto ``Class.decorators`` / ``Function.decorators``, so this detector just reads
the ``FileRecord`` — no AST re-walk, no new grammar.

Emitted as ``route`` statements:
* ``semanticType=route``, ``framework=wcf``, ``handler`` = the operation method.
* ``endpoint`` = ``ServiceName/OperationName`` (SOAP/RPC); for REST-over-WCF the served address
  ``<.svc path>/<UriTemplate or Operation>`` when the class's ``.svc`` is found, else empty.
* ``method`` = ``RPC`` for SOAP operations; the HTTP verb for ``[WebGet]``/``[WebInvoke]``.
* ``routeKind`` = ``rpc`` (SOAP) or ``route`` (REST-over-WCF).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ...schemas import Class, Decorator, FileRecord, Function
from ..dotnet_common import code_behind_host, response_dto, simple_attr_name
from ..emits import Emit
from .svc_hosts import SVC_SERVICE_RE, svc_hosts

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


#: The other named arguments of ``[WebGet]`` / ``[WebInvoke]`` are enum values.
_WEB_ENUMS = ("WebMessageFormat.", "WebMessageBodyStyle.")


def _uri_arg(dec: Decorator) -> str | None:
    """The ``UriTemplate`` value of a WebGet/WebInvoke. Its other arguments are ``Method`` (an
    HTTP verb) and the format / body-style enums, so the template is the one argument that is
    neither — ``"orders/{id}"``, ``"/status"`` or a bare ``"status"``."""
    for a in dec.args:
        s = a.strip()
        name, eq, value = s.partition("=")
        if eq and name.strip().isidentifier():  # kept in `Name = value` form
            if name.strip() == "UriTemplate":
                return value.strip().strip('"')
            continue
        if s and s.upper() not in _HTTP_VERBS and not s.startswith(_WEB_ENUMS):
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


def _on_operation(fn: Function, text: str) -> dict[str, Any]:
    """Where an operation's record sits: read off the record, so the file's root stands in for
    a node and the span is the operation method's."""
    return dict(parent=fn.id, node_type="synthetic", text=text, line=fn.startLine, col=0,
                end_line=fn.endLine)


def _implementing_handler(
    cls_name: str, op: str, record: FileRecord, index: object | None
) -> tuple[str, int | None] | None:
    """For a contract declared on an interface: the method with a body that runs —
    ``(Class.Method, handlerLine)`` when exactly one type in the repo implements the interface
    and declares that method; else None (the interface method stays the handler). handlerLine
    is set only when the implementing method is in this file (no ``handlerPath`` yet)."""
    implementers = getattr(index, "implementers", None)
    heritage_map = getattr(index, "class_heritage", None)
    if not isinstance(implementers, dict) or not isinstance(heritage_map, dict):
        return None
    impl = implementers.get(cls_name)
    if impl is None:
        return None
    simple = impl.rsplit(".", 1)[-1]
    heritage = heritage_map.get(simple)
    if heritage is None or op not in heritage.methods:
        return None
    impl_cls = next((c for c in record.classes if c.name == simple), None)
    line = next((f.startLine for f in record.functions
                 if impl_cls is not None and f.parentId == impl_cls.id and f.name == op), None)
    return f"{simple}.{op}", line


def _registered_host(cls: Class, path: str, index: object | None) -> str | None:
    """The ``.svc`` serving ``cls`` from the repo's ``wcf.svc-hosts`` fact: a class by its own
    type, an interface contract by its one implementing type (``index.implementers``)."""
    hosts = svc_hosts(index)
    if not hosts.by_fqn:
        return None
    if cls.type == "interface":
        implementers = getattr(index, "implementers", None)
        impl = implementers.get(cls.name) if isinstance(implementers, dict) else None
        return hosts.host(impl)
    types = getattr(index, "types", None)
    fqns = [fqn for fqn, files in (types or {}).items()
            if fqn.rsplit(".", 1)[-1] == cls.name and path in files] if isinstance(types, dict) else []
    return hosts.host(fqns[0] if len(fqns) == 1 else None)


def detect_wcf_services(
    record: FileRecord, root: Node, repo_root: Path | None = None, index: object | None = None
) -> list[Emit]:
    """One ``route`` statement per ``[OperationContract]`` under a ``[ServiceContract]`` type
    (WCF / CoreWCF; tolerant of the full ``…Attribute`` form). A REST operation's endpoint is
    its served address under the class's ``.svc`` (:func:`svc_host`, else the ``wcf.svc-hosts``
    fact — a ``.svc`` anywhere naming the class or the interface's implementer), empty when that
    host is not known — a bare ``UriTemplate`` or a service name is not an address. For a contract on an
    interface, the handler is the implementing class's method when the repo has exactly one
    implementer (``index.implementers``); otherwise the interface method."""
    contracts = [
        cls for cls in record.classes
        if _has_attr(cls.decorators, _SERVICE_ATTR) and not _has_attr(cls.decorators, _GENERATED_ATTR)
    ]
    if not contracts:
        return []
    services: dict[str, str] = {cls.id: _service_name(cls.decorators, cls.name) for cls in contracts}
    hosts: dict[str, str | None] = {
        cls.id: svc_host(cls.path or record.path, repo_root, cls.name)
        or _registered_host(cls, record.path, index)
        for cls in contracts
    }
    interfaces = {cls.id: cls.name for cls in contracts if cls.type == "interface"}
    web_hosts = {cid: _web_host(host, repo_root) for cid, host in hosts.items()}

    routes: list[Emit] = []
    for fn in record.functions:
        svc = services.get(fn.parentId)
        if svc is None or not _has_attr(fn.decorators, _OP_ATTR):
            continue
        web = next((d for d in fn.decorators if simple_attr_name(d.name) in (_WEB_GET, _WEB_INVOKE)), None)
        if web is not None:  # REST-over-WCF (WebHttpBinding)
            method = "GET" if simple_attr_name(web.name) == _WEB_GET else (_verb_arg(web) or "POST")
            host = hosts.get(fn.parentId)
            endpoint = _rest_address(host, _uri_arg(web), fn.name) if host is not None else None
            route_kind = "route"
        elif web_hosts.get(fn.parentId):  # an HTTP host: POST <file>.svc/<Operation>
            method, route_kind = "POST", "route"
            endpoint = _rest_address(hosts[fn.parentId] or "", None, fn.name)
        else:  # SOAP / net.tcp RPC
            method, endpoint, route_kind = "RPC", f"{svc}/{fn.name}", "rpc"
        handler: str = fn.name
        handler_line: int | None = fn.startLine
        iface = interfaces.get(fn.parentId)
        impl = _implementing_handler(iface, fn.name, record, index) if iface else None
        if impl is not None:
            handler, handler_line = impl
        routes.append(
            Emit(root, dict(
                semanticType="route",
                method=method,
                endpoint=endpoint,
                framework="wcf",
                handler=handler,
                handlerLine=handler_line,
                routeKind=route_kind,
                isRegex=False,
                responseDTO=response_dto(fn.returnType),
            ), **_on_operation(fn, f"[{_OP_ATTR}] {svc}.{fn.name}"))
        )
    return routes


def asmx_host(path: str, repo_root: Path | None, cls_name: str) -> str | None:
    """The ``.asmx`` file serving class ``cls_name`` (``X.asmx.cs`` beside ``X.asmx``), or None."""
    return code_behind_host(path, repo_root, cls_name, ".asmx", _ASMX_CLASS_RE)


def svc_host(path: str, repo_root: Path | None, cls_name: str) -> str | None:
    """The ``.svc`` file serving class ``cls_name`` (``X.svc.cs`` beside ``X.svc``, its
    ``ServiceHost Service=`` naming the class), or None. An interface contract, or a class whose
    ``.svc`` is elsewhere, has no known host here."""
    return code_behind_host(path, repo_root, cls_name, ".svc", SVC_SERVICE_RE)


#: ``Factory="…WebScriptServiceHostFactory"`` (AJAX-enabled WCF) / ``WebServiceHostFactory``
#: (REST): the host serves every operation over HTTP — ``POST <file>.svc/<Operation>`` unless
#: ``[WebGet]`` / ``[WebInvoke]`` says otherwise (WCF's default for these hosts).
_WEB_FACTORY_RE = re.compile(
    rb'<%@\s*ServiceHost\b[^%]*?\bFactory\s*=\s*["\'][\w.]*\bWeb(?:Script)?ServiceHostFactory["\']',
    re.IGNORECASE)


def _web_host(host: str | None, repo_root: Path | None) -> bool:
    """Whether the ``.svc`` at ``host`` serves its operations over HTTP (:data:`_WEB_FACTORY_RE`)."""
    if host is None or repo_root is None:
        return False
    try:
        return _WEB_FACTORY_RE.search((repo_root / host).read_bytes()) is not None
    except OSError:
        return False


def _rest_address(host: str, template: str | None, operation: str) -> str:
    """``/<.svc path>/<UriTemplate>`` (its query part dropped — those are query bindings, not the
    path), or ``/<.svc path>/<Operation>`` when the operation has no ``UriTemplate`` (WCF's default
    for WebGet / WebInvoke). REST over WCF is HTTP, so the address is a path with a leading
    ``/``, like a Web Forms page's (contract §6); SOAP keeps ``<Service>.asmx/<Operation>``."""
    tail = template.split("?", 1)[0].strip("/") if template else operation
    return "/" + (f"{host}/{tail}" if tail else host).lstrip("/")


def detect_asmx_services(
    record: FileRecord, root: Node, repo_root: Path | None = None,
) -> list[Emit]:
    """Legacy ASMX SOAP web services (``System.Web.Services``): each ``[WebMethod]`` method is
    an operation. ``[WebService]`` on the class is optional in ASMX, so the ``[WebMethod]``
    methods themselves drive detection. Emitted like WCF SOAP ops (``framework=asmx``, ``rpc``).
    ``endpoint`` is the served address ``<.asmx path>/<Method>`` (the ``.asmx`` path is
    repo-relative, as on the host record) when the ``.asmx`` file is found
    (:func:`asmx_host`); otherwise empty — a service or class name is not an address."""
    cls_by_id = {c.id: c for c in record.classes}
    routes: list[Emit] = []
    for fn in record.functions:
        if not _has_attr(fn.decorators, _WEB_METHOD_ATTR):
            continue
        cls = cls_by_id.get(fn.parentId)
        if cls is not None and _has_attr(cls.decorators, _GENERATED_ATTR):
            continue  # generated proxy, not a real ASMX service
        svc = (_name_arg(cls.decorators, _WEB_SERVICE_ATTR) or cls.name) if cls is not None else "WebService"
        host = asmx_host(fn.path or record.path, repo_root, cls.name) if cls is not None else None
        routes.append(
            Emit(root, dict(
                semanticType="route",
                method="RPC",
                endpoint=f"{host}/{fn.name}" if host is not None else None,
                framework="asmx",
                handler=fn.name,
                handlerLine=fn.startLine,
                routeKind="rpc",
                isRegex=False,
                responseDTO=response_dto(fn.returnType),
            ), **_on_operation(fn, f"[{_WEB_METHOD_ATTR}] {svc}.{fn.name}"))
        )
    return routes
