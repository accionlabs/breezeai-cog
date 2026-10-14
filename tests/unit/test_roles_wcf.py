"""Role-table tests for WCF and ASMX (docs/entry-point-roles/wcf.md)."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point

import pytest

EP = {"route", "eventbus_consumer", "timer"}

CONTRACT = """using System.ServiceModel;
using System.ServiceModel.Web;

namespace Shop
{
    [ServiceContract]
    public interface IOrderService
    {
        [OperationContract]
        Order GetOrder(int id);

        [OperationContract]
        [WebGet(UriTemplate = "/orders/{id}")]
        Order FindOrder(string id);

        int Helper();
    }
}
"""

ASMX_CB = """using System.Web.Services;

namespace Shop
{
    [WebService(Namespace = "http://shop.example/")]
    public class Orders : WebService
    {
        [WebMethod]
        public string GetOrder(int id) { return ""; }
    }
}
"""

PROXY = """using System.CodeDom.Compiler;
using System.ServiceModel;

namespace Shop.Client
{
    [GeneratedCode("System.ServiceModel", "4.0.0.0")]
    [ServiceContract]
    public interface IOrderService
    {
        [OperationContract]
        string GetOrder(int id);
    }
}
"""


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _line(src: str, snippet: str) -> int:
    return next(i for i, line in enumerate(src.splitlines(), 1) if snippet in line)


@pytest.fixture
def wcf(analyze_files):
    return analyze_files(
        {
            "Services/IOrderService.cs": CONTRACT,
            "Services/OrderService.svc": '<%@ ServiceHost Service="Shop.OrderService" %>',
        }
    )


def test_operation_contract_is_binding(wcf) -> None:
    [r] = [s for s in _eps(wcf) if s.handler == "GetOrder"]
    assert (r.semanticType, r.method, r.routeKind, r.framework) == ("route", "RPC", "rpc", "wcf")
    assert r.endpoint == "OrderService/GetOrder"
    assert r.handlerLine == _line(CONTRACT, "[OperationContract]")


def test_service_contract_is_wiring(wcf) -> None:
    line = _line(CONTRACT, "public interface IOrderService")
    assert [s for s in _eps(wcf) if s.startLine == line] == []


def test_method_without_operation_contract_is_none(wcf) -> None:
    assert [s for s in _eps(wcf) if s.handler == "Helper"] == []


def test_webget_operation_is_http_binding(wcf) -> None:
    [r] = [s for s in _eps(wcf) if s.handler == "FindOrder"]
    assert (r.method, r.routeKind) == ("GET", "route")


IMPL = """namespace Shop
{
    public class OrderService : IOrderService
    {
        public Order GetOrder(int id) { return null; }
        public Order FindOrder(string id) { return null; }
        public int Helper() { return 0; }
    }
}
"""


def test_webget_endpoint_includes_svc_path(analyze_files) -> None:
    # The .svc names the implementing class; the contract is its interface, in another file.
    st = analyze_files({
        "Services/IOrderService.cs": CONTRACT,
        "Services/Impl/OrderService.cs": IMPL,
        "Services/OrderService.svc": '<%@ ServiceHost Service="Shop.OrderService" %>',
    })
    [r] = [s for s in _eps(st) if s.endpoint and "orders" in s.endpoint]
    assert (r.endpoint, r.handler) == ("/Services/OrderService.svc/orders/{id}", "OrderService.FindOrder")


def test_svc_in_another_folder_hosts_a_contract_class(analyze_files) -> None:
    contract_class = CONTRACT.replace("public interface IOrderService", "public class OrderService") \
        .replace("Order GetOrder(int id);", "public Order GetOrder(int id) { return null; }") \
        .replace("Order FindOrder(string id);", "public Order FindOrder(string id) { return null; }") \
        .replace("int Helper();", "public int Helper() { return 0; }")
    st = analyze_files({
        "src/Shop/OrderService.cs": contract_class,
        "web/api/Orders.svc": '<%@ ServiceHost Service="Shop.OrderService" %>',
    })
    [r] = [s for s in _eps(st) if s.handler == "FindOrder"]
    assert r.endpoint == "/web/api/Orders.svc/orders/{id}"


def test_svc_without_implementer_leaves_the_address_empty(wcf) -> None:
    # No class implements IOrderService, so nothing links it to the .svc: empty, never guessed.
    [r] = [s for s in _eps(wcf) if s.handler == "FindOrder"]
    assert r.endpoint is None


def test_svc_host_directive_is_an_rpc_route(wcf) -> None:
    # The Target Spec's current form: the directive is a route, RPC, endpoint = the .svc file.
    [r] = [s for s in _eps(wcf) if s.path == "Services/OrderService.svc"]
    assert (r.method, r.routeKind, r.endpoint) == ("RPC", "rpc", "Services/OrderService.svc")


def test_svc_host_keeps_codebehind_link(analyze_files, tmp_path) -> None:
    from breezeai_cog import iter_file_records

    analyze_files(
        {
            "OrderService.svc": '<%@ ServiceHost Service="Shop.OrderService" CodeBehind="OrderService.svc.cs" %>',
            "OrderService.svc.cs": "namespace Shop { public class OrderService { } }",
        }
    )
    records = {r.path: r for r in iter_file_records(tmp_path / "repo")}
    assert records["OrderService.svc"].importFiles == ["OrderService.svc.cs"]


def test_asmx_web_method_is_binding(analyze_files) -> None:
    stmts = analyze_files({"Orders.asmx.cs": ASMX_CB})
    [r] = _eps(stmts)
    assert (r.method, r.routeKind, r.framework, r.handler) == ("RPC", "rpc", "asmx", "GetOrder")


def test_asmx_endpoint_uses_asmx_file(analyze_files) -> None:
    stmts = analyze_files(
        {
            "Orders.asmx": '<%@ WebService Class="Shop.Orders" CodeBehind="Orders.asmx.cs" %>',
            "Orders.asmx.cs": ASMX_CB,
        }
    )
    [r] = [s for s in _eps(stmts) if s.path == "Orders.asmx.cs"]
    assert r.endpoint == "Orders.asmx/GetOrder"


def test_asmx_host_directive_is_an_rpc_route(analyze_files) -> None:
    # The Target Spec's current form: the directive is a route, RPC, endpoint = the .asmx file.
    stmts = analyze_files(
        {
            "Orders.asmx": '<%@ WebService Class="Shop.Orders" CodeBehind="Orders.asmx.cs" %>',
            "Orders.asmx.cs": ASMX_CB,
        }
    )
    [r] = [s for s in _eps(stmts) if s.path == "Orders.asmx"]
    assert (r.method, r.routeKind, r.endpoint, r.handler) == ("RPC", "rpc", "Orders.asmx", "Shop.Orders")


def test_generated_client_proxy_is_outbound(analyze_files) -> None:
    stmts = analyze_files({"Client/Reference.cs": PROXY})
    assert _eps(stmts) == []
