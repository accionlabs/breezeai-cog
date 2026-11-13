"""WCF and ASMX services in VB: the C# rules (``wcf_common``), with the handler read from the
``Implements`` clause and the host from the ``.svc`` / ``.asmx`` beside the class."""

from __future__ import annotations

from breezeai_cog.core import registry
from breezeai_cog.parsers.vb.parser import VbParser
from breezeai_cog.parsers.vb_aspnet.parser import VbAspNetParser
from breezeai_cog.parsers.vb_wcf.parser import VbWcfParser
from breezeai_cog.parsers.vb_webforms.parser import VbWebFormsParser

CONTRACT = """Imports System.ServiceModel
Imports System.ServiceModel.Web

<ServiceContract(Name:="Orders")>
Public Interface IOrderService
    <OperationContract()>
    Function GetOrder(id As String) As Order

    <OperationContract(), WebGet(UriTemplate:="orders/{id}?full={full}")>
    Function GetOrderJson(id As String, full As Boolean) As Order

    <OperationContract>
    <WebInvoke(Method:="PUT", UriTemplate:=Routes.Save)>
    Sub SaveOrder(o As Order)

    Sub NotExposed()
End Interface
"""

IMPLEMENTATION = """Namespace Shop
Public Class OrderService
    Implements IOrderService

    Public Function Fetch(id As String) As Order Implements IOrderService.GetOrder
        Return Nothing
    End Function
    Public Function GetOrderJson(id As String, full As Boolean) As Order Implements IOrderService.GetOrderJson
        Return Nothing
    End Function
    Public Sub SaveOrder(o As Order) Implements IOrderService.SaveOrder
    End Sub
    Public Sub NotExposed() Implements IOrderService.NotExposed
    End Sub
End Class
End Namespace
"""


def _routes(stmts, path: str) -> set[tuple]:
    return {(s.routeKind, s.method, s.endpoint, s.handler, s.handlerLine, s.handlerPath, s.framework)
            for s in stmts if s.semanticType == "route" and s.path == path}


def test_vb_service_files_are_claimed_and_pages_are_not() -> None:
    registry.clear()
    for p in (VbParser(), VbAspNetParser(), VbWcfParser(), VbWebFormsParser()):
        registry.register(p)
    assert registry.select("IOrders.vb", b"Imports System.ServiceModel\n").name == "vb-wcf"
    # A project-wide import: the attribute alone claims the file.
    assert registry.select("IOrders.vb", b"<ServiceContract()>\nInterface IOrders\n").name == "vb-wcf"
    assert registry.select("Legacy.asmx.vb", b"<WebMethod()> Function F()\n").name == "vb-wcf"
    # A page's <WebMethod> is a page method.
    assert registry.select("Orders.aspx.vb", b"Imports System.Web.Services\n").name == "vb-webforms"
    assert registry.select("Plain.vb", b"Public Class Plain\nEnd Class\n").name == "vb"


def test_a_vb_contract_runs_through_the_method_its_implementer_names(analyze_files) -> None:
    st = analyze_files({
        "Contracts/IOrderService.vb": CONTRACT,
        "Services/OrderService.svc": (
            '<%@ ServiceHost Language="VB" Service="Shop.OrderService" '
            'CodeBehind="OrderService.svc.vb" %>\n'),
        "Services/OrderService.svc.vb": IMPLEMENTATION,
    })
    impl = "Services/OrderService.svc.vb"
    assert _routes(st, "Contracts/IOrderService.vb") == {
        # SOAP: the implementing method is named differently (Fetch), found through Implements.
        ("rpc", "RPC", "Orders/GetOrder", "OrderService.Fetch", 5, impl, "wcf"),
        # REST: the .svc beside the implementing class; the query part is not the path.
        ("route", "GET", "/Services/OrderService.svc/orders/{id}", "OrderService.GetOrderJson", 8,
         impl, "wcf"),
        # A UriTemplate that is not a literal: no endpoint (missing, not wrong).
        ("route", "PUT", None, "OrderService.SaveOrder", 11, impl, "wcf"),
    }


def test_two_implementers_leave_the_interface_method_as_handler(analyze_files) -> None:
    other = IMPLEMENTATION.replace("Class OrderService", "Class OtherService") \
        .replace("Fetch", "Lookup")
    st = analyze_files({
        "Contracts/IOrderService.vb": CONTRACT,
        "Services/OrderService.vb": IMPLEMENTATION,
        "Services/OtherService.vb": other,
    })
    got = {(s.endpoint, s.handler, s.handlerLine, s.handlerPath)
           for s in st if s.routeKind == "rpc"}
    assert got == {("Orders/GetOrder", "GetOrder", 6, None)}
    # No .svc beside either class: REST operations have no address.
    assert {s.endpoint for s in st if s.routeKind == "route"} == {None}


def test_a_vb_class_contract_on_a_web_host_and_a_generated_proxy(analyze_files) -> None:
    st = analyze_files({
        "Api/Ajax.svc": ('<%@ ServiceHost Service="Shop.Ajax" CodeBehind="Ajax.svc.vb" '
                         'Factory="System.ServiceModel.Activation.WebScriptServiceHostFactory" %>\n'),
        "Api/Ajax.svc.vb": (
            "<ServiceContract()>\n"
            "Public Class Ajax\n"
            "    <OperationContract()>\n"
            "    Public Function Ping() As String\n"
            '        Return "pong"\n'
            "    End Function\n"
            "End Class\n"),
        "Proxies/OrdersClient.vb": (
            "Imports System.ServiceModel\n"
            '<System.CodeDom.Compiler.GeneratedCode("System.ServiceModel", "4.0.0.0"), ServiceContract()>\n'
            "Public Interface IOrdersClient\n"
            "    <OperationContract()> Function GetOrder(id As String) As Order\n"
            "End Interface\n"),
    })
    assert _routes(st, "Api/Ajax.svc.vb") == {
        ("route", "POST", "/Api/Ajax.svc/Ping", "Ping", 3, None, "wcf")}
    assert not _routes(st, "Proxies/OrdersClient.vb")


def test_vb_asmx_web_methods(analyze_files) -> None:
    st = analyze_files({
        "Services/Legacy.asmx": '<%@ WebService Language="VB" CodeBehind="Legacy.asmx.vb" Class="Shop.Legacy" %>\n',
        "Services/Legacy.asmx.vb": (
            "Imports System.Web.Services\n"
            '<WebService(Namespace:="http://tempuri.org/")>\n'
            "Public Class Legacy\n"
            "    Inherits System.Web.Services.WebService\n"
            "    <WebMethod()>\n"
            "    Public Function HelloWorld() As String\n"
            '        Return "Hello"\n'
            "    End Function\n"
            "    Public Function NotExposed() As String\n"
            '        Return ""\n'
            "    End Function\n"
            "End Class\n"),
    })
    assert _routes(st, "Services/Legacy.asmx.vb") == {
        ("rpc", "RPC", "Services/Legacy.asmx/HelloWorld", "HelloWorld", 5, None, "asmx")}
