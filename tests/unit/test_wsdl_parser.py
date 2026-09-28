"""Standalone WSDL 1.1 (SOAP service contract) capture.

The ``wsdl`` language parser owns ``.wsdl`` files. It emits one ``data_model`` statement per
``message``, one ``route`` statement per operation — ``portType/operation`` correlated with
``binding/operation`` via the binding's ``type=`` QName (not a bare operation-name match, to
avoid merging same-named operations under two different ``portType``s), one ``import``
statement per ``wsdl:import`` (never resolved), and reuses the ``xsd`` walker for every
``<xsd:schema>`` embedded in ``<wsdl:types>``. Capture is gated on ``--capture-statements``.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.wsdl.parser import WsdlParser
from breezeai_cog.schemas import FileRecord

WSDL = """\
<?xml version="1.0"?>
<wsdl:definitions name="UserService"
    targetNamespace="http://example.com/user"
    xmlns:tns="http://example.com/user"
    xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
    xmlns:xs="http://www.w3.org/2001/XMLSchema">

  <!-- shared contract -->
  <wsdl:import namespace="http://example.com/common" location="common.wsdl"/>

  <wsdl:types>
    <xs:schema targetNamespace="http://example.com/user" xmlns:xs="http://www.w3.org/2001/XMLSchema">
      <xs:complexType name="User">
        <xs:sequence>
          <xs:element name="id" type="xs:int"/>
          <xs:element name="name" type="xs:string"/>
        </xs:sequence>
      </xs:complexType>
    </xs:schema>
  </wsdl:types>

  <wsdl:message name="GetUserRequest">
    <wsdl:part name="id" type="xs:int"/>
  </wsdl:message>
  <wsdl:message name="GetUserResponse">
    <wsdl:part name="user" type="tns:User"/>
  </wsdl:message>

  <wsdl:portType name="UserServicePortType">
    <wsdl:operation name="GetUser">
      <wsdl:input message="tns:GetUserRequest"/>
      <wsdl:output message="tns:GetUserResponse"/>
    </wsdl:operation>
  </wsdl:portType>

  <wsdl:binding name="UserServiceBinding" type="tns:UserServicePortType">
    <soap:binding transport="http://schemas.xmlsoap.org/soap/http" style="rpc"/>
    <wsdl:operation name="GetUser">
      <soap:operation soapAction="http://example.com/user/GetUser"/>
      <wsdl:input><soap:body use="literal"/></wsdl:input>
      <wsdl:output><soap:body use="literal"/></wsdl:output>
    </wsdl:operation>
  </wsdl:binding>

  <wsdl:service name="UserService">
    <wsdl:port name="UserServicePort" binding="tns:UserServiceBinding">
      <soap:address location="http://example.com/UserService"/>
    </wsdl:port>
  </wsdl:service>
</wsdl:definitions>
"""


def _parse(tmp_path, filename: str, src: str, *, capture: bool = True) -> FileRecord:
    p = tmp_path / filename
    p.write_text(src)
    ctx = ParseContext(
        path=filename,
        abs_path=p,
        source=src.encode(),
        repo_root=tmp_path,
        capture_statements=capture,
        statement_text_limit=1000,
    )
    return WsdlParser().parse_file(ctx)


def _by_name(rec, name: str, node_type: str):
    return next(s for s in rec.statements if s.name == name and s.nodeType == node_type)


def test_language_and_extensions() -> None:
    p = WsdlParser()
    assert p.name == "wsdl"
    assert p.extensions == (".wsdl",)
    assert "wsdl" in p.frameworks


def test_message_is_data_model_entity(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    assert rec.language == "wsdl"
    assert rec.framework == "wsdl"
    req = _by_name(rec, "GetUserRequest", "message")
    assert req.semanticType == "data_model"
    assert req.parentId == rec.id
    assert 'name="id"' in req.text


def test_operation_is_rpc_route_with_soap_action(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    op = _by_name(rec, "GetUser", "synthetic")
    assert op.semanticType == "route"
    assert op.framework == "wsdl"
    assert op.routeKind == "rpc"
    assert op.method == "http://example.com/user/GetUser"
    assert op.endpoint == "UserServicePortType/GetUser"
    assert op.requestDTO == "GetUserRequest"
    assert op.responseDTO == "GetUserResponse"


def test_operation_without_soap_action_falls_back_to_rpc(tmp_path) -> None:
    src = WSDL.replace('<soap:operation soapAction="http://example.com/user/GetUser"/>', "<soap:operation/>")
    rec = _parse(tmp_path, "service.wsdl", src)
    op = _by_name(rec, "GetUser", "synthetic")
    assert op.method == "RPC"


def test_second_binding_for_same_operation_not_duplicated(tmp_path) -> None:
    # Regression: real-world WSDLs (verified against an ASP.NET-generated example)
    # routinely declare two bindings for the same portType -- a SOAP 1.1 `soap:binding` and
    # a SOAP 1.2 `soap12:binding` -- purely for wire-protocol compatibility. Without
    # dedup, every operation was emitted once per binding.
    src = """\
<?xml version="1.0"?>
<wsdl:definitions
    xmlns:tns="http://example.com/calc"
    xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
    xmlns:soap12="http://schemas.xmlsoap.org/wsdl/soap12/">
  <wsdl:message name="AddRequest"/>
  <wsdl:message name="AddResponse"/>
  <wsdl:portType name="CalculatorSoap">
    <wsdl:operation name="Add">
      <wsdl:input message="tns:AddRequest"/>
      <wsdl:output message="tns:AddResponse"/>
    </wsdl:operation>
  </wsdl:portType>
  <wsdl:binding name="CalculatorSoap" type="tns:CalculatorSoap">
    <soap:binding transport="http://schemas.xmlsoap.org/soap/http"/>
    <wsdl:operation name="Add">
      <soap:operation soapAction="http://tempuri.org/Add"/>
    </wsdl:operation>
  </wsdl:binding>
  <wsdl:binding name="CalculatorSoap12" type="tns:CalculatorSoap">
    <soap12:binding transport="http://schemas.xmlsoap.org/soap/http"/>
    <wsdl:operation name="Add">
      <soap12:operation soapAction="http://tempuri.org/Add"/>
    </wsdl:operation>
  </wsdl:binding>
</wsdl:definitions>
"""
    rec = _parse(tmp_path, "service.wsdl", src)
    routes = [s for s in rec.statements if s.nodeType == "synthetic"]
    assert len(routes) == 1
    assert routes[0].endpoint == "CalculatorSoap/Add"


def test_binding_operation_scoped_to_its_own_port_type(tmp_path) -> None:
    # Two portTypes each declare an operation named "Ping" with different messages; the
    # binding references only PortA. A bare name match would risk merging PortB's messages
    # in — the binding's `type=` QName must scope the correlation to PortA only.
    src = """\
<?xml version="1.0"?>
<wsdl:definitions
    xmlns:tns="http://example.com/x"
    xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/">
  <wsdl:message name="PingARequest"/>
  <wsdl:message name="PingAResponse"/>
  <wsdl:message name="PingBRequest"/>
  <wsdl:message name="PingBResponse"/>
  <wsdl:portType name="PortA">
    <wsdl:operation name="Ping">
      <wsdl:input message="tns:PingARequest"/>
      <wsdl:output message="tns:PingAResponse"/>
    </wsdl:operation>
  </wsdl:portType>
  <wsdl:portType name="PortB">
    <wsdl:operation name="Ping">
      <wsdl:input message="tns:PingBRequest"/>
      <wsdl:output message="tns:PingBResponse"/>
    </wsdl:operation>
  </wsdl:portType>
  <wsdl:binding name="BindingA" type="tns:PortA">
    <wsdl:operation name="Ping">
      <soap:operation soapAction="urn:ping-a"/>
    </wsdl:operation>
  </wsdl:binding>
</wsdl:definitions>
"""
    rec = _parse(tmp_path, "service.wsdl", src)
    routes = [s for s in rec.statements if s.nodeType == "synthetic"]
    assert len(routes) == 1
    assert routes[0].endpoint == "PortA/Ping"
    assert routes[0].requestDTO == "PingARequest"
    assert routes[0].responseDTO == "PingAResponse"


def test_embedded_xsd_schema_reuses_xsd_walker(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    user = _by_name(rec, "User", "complexType")
    assert user.semanticType == "data_model"
    assert 'name="id"' in user.text


def test_import_recorded_not_resolved(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    imp = next(s for s in rec.statements if s.nodeType == "import")
    assert imp.semanticType is None
    assert imp.name == "http://example.com/common"


def test_comment_captured(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    comments = [s for s in rec.statements if s.nodeType == "Comment"]
    assert any("shared contract" in s.text for s in comments)


def test_capture_gate(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL, capture=False)
    assert rec.statements == []
    assert rec.language == "wsdl"
    assert rec.framework is None


def test_malformed_markup_yields_no_statements_not_a_crash(tmp_path) -> None:
    # Unclosed tag: the whole document fails to reduce to a valid `<definitions>` root
    # (same document-wide grammar-recovery behavior verified for `.xsd`) — a clean, empty
    # FileRecord, not a crash or guessed partial structure.
    src = (
        '<?xml version="1.0"?>\n'
        '<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/">\n'
        '  <wsdl:message name="Broken">\n'
        "</wsdl:definitions>\n"
    )
    rec = _parse(tmp_path, "service.wsdl", src)
    assert rec.statements == []


def test_records_validate_against_schema(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    errors = list(validator.iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
    assert rec.statements  # non-empty


def test_registered_and_selected_by_extension() -> None:
    registry.clear()
    registry.discover_builtin()
    sel = registry.select("service.wsdl", WSDL.encode())
    assert sel is not None and sel.name == "wsdl"
    assert ".wsdl" in registry.capabilities()["extensions"]
    assert "wsdl" in registry.capabilities()["languages"]
    registry.clear()
