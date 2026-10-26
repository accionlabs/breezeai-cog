"""Role table: docs/entry-point-roles/wsdl.md — one test per row."""

from __future__ import annotations

from breezeai_cog.parsers.contract import is_entry_point


def _eps(stmts):
    return [s for s in stmts if is_entry_point(s)]


def _wsdl(body: str) -> str:
    return (
        '<?xml version="1.0"?>\n'
        '<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"\n'
        '    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/" xmlns:tns="urn:shop">\n'
        f"{body}"
        "</wsdl:definitions>\n"
    )


PORT_TYPE = (
    '  <wsdl:message name="GetOrderRequest"/>\n'
    '  <wsdl:message name="GetOrderResponse"/>\n'
    '  <wsdl:portType name="IOrderService">\n'
    '    <wsdl:operation name="GetOrder">\n'
    '      <wsdl:input message="tns:GetOrderRequest"/>\n'
    '      <wsdl:output message="tns:GetOrderResponse"/>\n'
    "    </wsdl:operation>\n"
    "  </wsdl:portType>\n"
)


def test_port_type_operation_is_wiring(analyze_files) -> None:
    st = analyze_files({"OrderService.wsdl": _wsdl(PORT_TYPE)})
    assert _eps(st) == []
    op = next(s for s in st if s.name == "GetOrder")
    assert (op.semanticType, op.endpoint, op.requestDTO, op.responseDTO, op.framework) == (
        None,
        "IOrderService/GetOrder",
        "GetOrderRequest",
        "GetOrderResponse",
        "wsdl",
    )


def test_operation_next_to_its_wcf_implementation_is_one_entry_point(analyze_files) -> None:
    st = analyze_files(
        {
            "Contracts/OrderService.wsdl": _wsdl(PORT_TYPE),
            "Service/OrderService.cs": (
                "using System.ServiceModel;\n"
                "namespace Shop\n{\n"
                "    [ServiceContract]\n"
                "    public interface IOrderService\n    {\n"
                "        [OperationContract]\n"
                "        Order GetOrder(int id);\n"
                "    }\n\n"
                "    public class OrderService : IOrderService\n    {\n"
                "        public Order GetOrder(int id) { return null; }\n"
                "    }\n}\n"
            ),
        }
    )
    assert [(s.framework, s.handler) for s in _eps(st)] == [("wcf", "OrderService.GetOrder")]


def test_binding_and_its_operations_are_none(analyze_files) -> None:
    binding = (
        '  <wsdl:binding name="OrderSoap" type="tns:IOrderService">\n'
        '    <soap:binding transport="http://schemas.xmlsoap.org/soap/http"/>\n'
        '    <wsdl:operation name="GetOrder">\n'
        '      <soap:operation soapAction="urn:GetOrder"/>\n'
        "    </wsdl:operation>\n"
        "  </wsdl:binding>\n"
    )
    st = analyze_files({"OrderService.wsdl": _wsdl(PORT_TYPE + binding)})
    assert _eps(st) == []
    # The binding's own `operation` adds nothing: one record, from the portType.
    assert [s.startLine for s in st if s.name == "GetOrder"] == [7]


def test_service_and_port_are_none(analyze_files) -> None:
    service = (
        '  <wsdl:service name="OrderService">\n'
        '    <wsdl:port name="OrderPort" binding="tns:OrderSoap">\n'
        '      <soap:address location="http://shop.example/orders"/>\n'
        "    </wsdl:port>\n"
        "  </wsdl:service>\n"
    )
    st = analyze_files({"OrderService.wsdl": _wsdl(service)})
    assert [s for s in st if s.semanticType != "comment"] == []


def test_message_is_a_data_model_not_an_entry_point(analyze_files) -> None:
    st = analyze_files({"OrderService.wsdl": _wsdl('  <wsdl:message name="GetOrderRequest"/>\n')})
    assert _eps(st) == []
    assert [(s.semanticType, s.endpoint) for s in st] == [("data_model", "GetOrderRequest")]


def test_import_is_none(analyze_files) -> None:
    st = analyze_files(
        {
            "OrderService.wsdl": _wsdl(
                '  <wsdl:import namespace="urn:common" location="common.wsdl"/>\n'
            )
        }
    )
    assert _eps(st) == []
    assert [(s.semanticType, s.name, s.endpoint) for s in st] == [(None, "urn:common", None)]
