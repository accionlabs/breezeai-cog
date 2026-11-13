"""WCF and ASMX operations, read off the record — shared by the ``csharp_wcf`` and ``vb_wcf``
parsers.

A WCF service is a ``[ServiceContract]``-decorated interface (or class); each ``[OperationContract]``
method is a callable operation (an entry point over SOAP/net.tcp, or REST when the method also
carries ``[WebGet]``/``[WebInvoke]``). The language parsers capture these attributes onto
``Class.decorators`` / ``Function.decorators`` (VB's ``<…>`` included), so the rules here read the
``FileRecord``; what needs the tree or the language's index is passed in by the language
(:class:`WcfLookups`): the ``.svc`` host of a contract, the ``UriTemplate`` of an operation, and
the method implementing an interface operation.

Emitted as ``route`` statements:
* ``semanticType=route``, ``framework=wcf``, ``handler`` = the operation method.
* ``endpoint`` = ``ServiceName/OperationName`` (SOAP/RPC); for REST-over-WCF the served address
  ``<.svc path>/<UriTemplate or Operation>`` when the class's ``.svc`` is found, else empty.
* ``method`` = ``RPC`` for SOAP operations; the HTTP verb for ``[WebGet]``/``[WebInvoke]``.
* ``routeKind`` = ``rpc`` (SOAP) or ``route`` (REST-over-WCF).

Legacy ASMX (``[WebMethod]``) operations are ``framework=asmx`` RPC routes at
``<.asmx path>/<Method>``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..schemas import Class, Decorator, FileRecord, Function
from .dotnet_common import code_behind_host, named_arg, response_dto, simple_attr_name
from .emits import Emit

SERVICE_ATTR = "ServiceContract"
OP_ATTR = "OperationContract"
WEB_GET = "WebGet"
WEB_INVOKE = "WebInvoke"
_WEB_SERVICE_ATTR = "WebService"   # ASMX (System.Web.Services)
_WEB_METHOD_ATTR = "WebMethod"     # ASMX operation
_GENERATED_ATTR = "GeneratedCode"  # svcutil-generated client proxy — NOT a server entry point
_HTTP_VERBS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
#: ``Service="Ns.Type"`` inside a ``.svc`` ``<%@ ServiceHost … %>`` directive.
SVC_SERVICE_RE = re.compile(
    rb'<%@\s*ServiceHost\b[^%]*?\bService\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE
)
#: ``Class="Ns.Type"`` inside an ``.asmx`` ``<%@ WebService … %>`` directive.
_ASMX_CLASS_RE = re.compile(rb'<%@\s*WebService\b[^%]*?\bClass\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)

#: A ``UriTemplate`` that is set but cannot be read (not a literal or a known constant).
UNKNOWN = object()

#: ``(handler, handlerLine, handlerPath)`` of the method that implements an interface operation.
Handler = tuple[str, int | None, str | None]


@dataclass(frozen=True)
class WcfLookups:
    """What the shared rules need from the language's tree and index.

    * ``host(cls)`` — the ``.svc`` path serving a contract (a class, or an interface by its one
      implementing class), or None;
    * ``uri_template(fn, web)`` — the ``UriTemplate`` of the operation's ``WebGet``/``WebInvoke``
      attribute ``web``: its value, None when it has none, :data:`UNKNOWN` when unreadable;
    * ``implementer(interface, fn)`` — the method that runs an interface operation, or None (the
      interface method stays the handler).
    """

    host: Callable[[Class], str | None]
    uri_template: Callable[[Function, Decorator], object]
    implementer: Callable[[str, Function], Handler | None]


def has_attr(decorators: list[Decorator], name: str) -> bool:
    """Whether a decorator with the given (short) attribute name is present — tolerant of
    the full ``…Attribute`` form (e.g. ``[ServiceContractAttribute]``)."""
    return any(simple_attr_name(d.name) == name for d in decorators)

# The C# and VB attribute readers keep a named argument as written (``Name = "X"`` /
# ``Name:="X"``) and a positional string unquoted; ``named_arg`` splits the key off.


def _verb_arg(dec: Decorator) -> str | None:
    """The ``Method = "…"`` value of a WebInvoke — the arg that is an HTTP verb."""
    for a in dec.args:
        key, value = named_arg(a)
        s = value.strip('"').strip().upper()
        if key in (None, "Method") and s in _HTTP_VERBS:
            return s
    return None


def _name_arg(decorators: list[Decorator], attr: str) -> str | None:
    """The bare-identifier ``Name="X"`` value on the given attribute, if present (a plain
    identifier — not a namespace URI) — or a positional one of that shape."""
    for d in decorators:
        if simple_attr_name(d.name) == attr:
            for a in d.args:
                key, value = named_arg(a)
                s = value.strip('"').strip() if key == "Name" else (value if key is None else "")
                if s and not any(c in s for c in "=/.:"):
                    return s
    return None


def _service_name(decorators: list[Decorator], class_name: str) -> str:
    """The WCF service name: the ``[ServiceContract(Name="X")]`` value when present, else the
    interface name with a conventional leading ``I`` dropped (``IEnrollmentService`` →
    ``EnrollmentService``)."""
    name = _name_arg(decorators, SERVICE_ATTR)
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


def detect_wcf_services(
    record: FileRecord, root: Node, lookups: WcfLookups, repo_root: Path | None = None,
) -> list[Emit]:
    """One ``route`` statement per ``[OperationContract]`` under a ``[ServiceContract]`` type
    (WCF / CoreWCF; tolerant of the full ``…Attribute`` form). A REST operation's endpoint is
    its served address under the contract's ``.svc`` (``lookups.host``), empty when that host is
    not known — a bare ``UriTemplate`` or a service name is not an address — or when its
    ``UriTemplate`` cannot be read. For a contract on an interface, the handler is the method
    that implements the operation when the language finds exactly one (``lookups.implementer``);
    otherwise the interface method."""
    contracts = [
        cls for cls in record.classes
        if has_attr(cls.decorators, SERVICE_ATTR) and not has_attr(cls.decorators, _GENERATED_ATTR)
    ]
    if not contracts:
        return []
    services: dict[str, str] = {cls.id: _service_name(cls.decorators, cls.name) for cls in contracts}
    hosts: dict[str, str | None] = {cls.id: lookups.host(cls) for cls in contracts}
    interfaces = {cls.id: cls.name for cls in contracts if cls.type == "interface"}
    web_hosts = {cid: _web_host(host, repo_root) for cid, host in hosts.items()}

    routes: list[Emit] = []
    for fn in record.functions:
        svc = services.get(fn.parentId)
        if svc is None or not has_attr(fn.decorators, OP_ATTR):
            continue
        web = next((d for d in fn.decorators if simple_attr_name(d.name) in (WEB_GET, WEB_INVOKE)), None)
        if web is not None:  # REST-over-WCF (WebHttpBinding)
            method = "GET" if simple_attr_name(web.name) == WEB_GET else (_verb_arg(web) or "POST")
            host = hosts.get(fn.parentId)
            template = lookups.uri_template(fn, web)
            endpoint = None if host is None or template is UNKNOWN \
                else _rest_address(host, template, fn.name)
            route_kind = "route"
        elif web_hosts.get(fn.parentId):  # an HTTP host: POST <file>.svc/<Operation>
            method, route_kind = "POST", "route"
            endpoint = _rest_address(hosts[fn.parentId] or "", None, fn.name)
        else:  # SOAP / net.tcp RPC
            method, endpoint, route_kind = "RPC", f"{svc}/{fn.name}", "rpc"
        handler: str = fn.name
        handler_line: int | None = fn.startLine
        handler_path: str | None = None
        iface = interfaces.get(fn.parentId)
        impl = lookups.implementer(iface, fn) if iface else None
        if impl is not None:
            handler, handler_line, handler_path = impl
        routes.append(
            Emit(root, dict(
                semanticType="route",
                method=method,
                endpoint=endpoint,
                framework="wcf",
                handler=handler,
                handlerLine=handler_line,
                handlerPath=handler_path,
                routeKind=route_kind,
                isRegex=False,
                responseDTO=response_dto(fn.returnType),
            ), **_on_operation(fn, f"[{OP_ATTR}] {svc}.{fn.name}"))
        )
    return routes


def asmx_host(path: str, repo_root: Path | None, cls_name: str) -> str | None:
    """The ``.asmx`` file serving class ``cls_name`` (``X.asmx.cs`` / ``X.asmx.vb`` beside
    ``X.asmx``), or None."""
    return code_behind_host(path, repo_root, cls_name, ".asmx", _ASMX_CLASS_RE)


def svc_host(path: str, repo_root: Path | None, cls_name: str) -> str | None:
    """The ``.svc`` file serving class ``cls_name`` (``X.svc.cs`` / ``X.svc.vb`` beside ``X.svc``,
    its ``ServiceHost Service=`` naming the class), or None. An interface contract, or a class
    whose ``.svc`` is elsewhere, has no known host here."""
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


def _rest_address(host: str, template: object, operation: str) -> str:
    """``/<.svc path>/<UriTemplate>`` (its query part dropped — those are query bindings, not the
    path), or ``/<.svc path>/<Operation>`` when the operation has no ``UriTemplate`` (WCF's default
    for WebGet / WebInvoke). REST over WCF is HTTP, so the address is a path with a leading
    ``/``, like a Web Forms page's (contract §6); SOAP keeps ``<Service>.asmx/<Operation>``."""
    tail = template.split("?", 1)[0].strip("/") if isinstance(template, str) else operation
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
        if not has_attr(fn.decorators, _WEB_METHOD_ATTR):
            continue
        cls = cls_by_id.get(fn.parentId)
        if cls is not None and has_attr(cls.decorators, _GENERATED_ATTR):
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
