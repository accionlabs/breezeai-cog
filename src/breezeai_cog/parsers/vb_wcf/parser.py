"""VbWcfParser — WCF service contracts and ASMX web services in VB, by the C# rules
(:mod:`..wcf_common`)::

    <ServiceContract(Name:="Orders")>
    Public Interface IOrderService
        <OperationContract()> Function GetOrder(id As String) As Order
            → route RPC  Orders/GetOrder  handler OrderService.Fetch  (the method whose header says
              Implements IOrderService.GetOrder, in whatever file)
        <OperationContract(), WebGet(UriTemplate:="orders/{id}")> Function GetJson(id As String) As Order
            → route GET  /Services/OrderService.svc/orders/{id}   (the .svc beside the implementing class)

    <WebMethod()> Public Function HelloWorld() As String          ' Services/Legacy.asmx.vb
        → route RPC  Services/Legacy.asmx/HelloWorld  framework asmx

What differs from C#, by what VB lets the parser know:

* **handler** — from the ``Implements`` clause (the ``vb.interface-members`` fact), so a method
  named differently from the operation is found; two implementers → the interface method;
* **host** — only the ``.svc`` beside the class (``X.svc.vb``, its directive naming the class), for
  an interface through its one implementing class. A ``Service=`` name is not matched repo-wide:
  VB prefixes the project's root namespace implicitly, so it cannot be compared reliably;
* **UriTemplate** — a string literal; anything else (a ``Const``) leaves the endpoint empty.

Claimed on a WCF / ASMX import or on the attributes themselves (a VB project can import a
namespace project-wide, so the file need not name it). Web Forms code-behind is not claimed: a
page's ``<WebMethod>`` is a page method (``vb_webforms``).
"""

from __future__ import annotations

import re

from ...schemas import Class, Decorator, FileRecord, Function
from ..base import ParseContext
from ..dotnet_common import named_arg
from ..emits import apply
from ..vb.parser import VbParser
from ..vb.source import parse_vb
from ..wcf_common import (
    UNKNOWN, Handler, WcfLookups, detect_asmx_services, detect_wcf_services, svc_host,
)
from .implements import interface_members

_MARKERS = (b"System.ServiceModel", b"CoreWCF", b"System.Web.Services")
_ATTRIBUTES = re.compile(rb"(?i)<[^>\n]*\b(?:ServiceContract|WebMethod)\b")
_WEBFORMS_SUFFIXES = (".aspx.vb", ".ascx.vb", ".master.vb")
#: A VB string literal as an attribute argument's whole value (``""`` is an escaped quote).
_LITERAL = re.compile(r'^"((?:[^"]|"")*)"$')


def _uri_template(web: Decorator) -> object:
    """The ``UriTemplate:=`` of a WebGet / WebInvoke: a literal's value, None when absent,
    :data:`~..wcf_common.UNKNOWN` when it is anything else."""
    for arg in web.args:
        key, value = named_arg(arg)
        if key is not None and key.lower() == "uritemplate":
            m = _LITERAL.match(value.strip())
            return m.group(1).replace('""', '"') if m else UNKNOWN
    return None


class VbWcfParser(VbParser):
    name = "vb-wcf"
    priority = 10  # framework parser > base vb (0)
    frameworks = ["wcf", "asmx"]

    def claims(self, path: str, source: bytes) -> bool:
        if path.lower().endswith(_WEBFORMS_SUFFIXES):
            return False
        return any(m in source for m in _MARKERS) or _ATTRIBUTES.search(source) is not None

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        root = parse_vb(ctx.source, ctx.parse_timeout_micros).root_node
        record = self.extract(root, ctx)  # inherited VB extraction (one parse)
        if ctx.capture_statements:
            members = interface_members(ctx.resolution_index)

            def host(cls: Class) -> str | None:
                if cls.type != "interface":
                    return svc_host(cls.path or record.path, ctx.repo_root, cls.name)
                impl = members.implementer(cls.name)
                return svc_host(impl[1], ctx.repo_root, impl[0]) if impl is not None else None

            def implementer(iface: str, fn: Function) -> Handler | None:
                hit = members.member(iface, fn.name)
                if hit is None:
                    return None
                return f"{hit.cls}.{hit.method}", hit.line, (hit.path if hit.path != record.path else None)

            lookups = WcfLookups(host=host, uri_template=lambda fn, web: _uri_template(web),
                                 implementer=implementer)
            routes = detect_wcf_services(record, root, lookups, ctx.repo_root) \
                or detect_asmx_services(record, root, ctx.repo_root)
            if routes:
                apply(record, routes, ctx.source, ctx.path)
                record.framework = routes[0].fields["framework"]  # "wcf" or "asmx"
        return record
