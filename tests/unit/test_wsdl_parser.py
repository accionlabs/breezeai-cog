"""Standalone WSDL 1.1 (SOAP service contract) capture.

The ``wsdl`` language parser owns ``.wsdl`` files. It emits one ``data_model`` statement per
``message`` (``endpoint=name``, ``framework="wsdl"`` — matches Prisma's own ``data_model``
statements, the join key an operation's ``requestDTO``/``responseDTO`` resolves to), one
plain statement per operation declared under any ``portType`` — wiring, not an entry point
(the implementing code is the binding) — regardless of whether a same-file ``binding``
implements it (real-world WSDLs commonly separate the interface from its wiring across
files, or omit a binding entirely), one ``import`` statement per ``wsdl:import``
(its ``location``, like an embedded schema's ``schemaLocation``, also resolved into
``importFiles`` / ``externalImports``), and reuses the ``xsd`` walker (tagged ``framework="wsdl"``) for every
``<xsd:schema>`` embedded in ``<wsdl:types>``. Capture is on by default.
"""

from __future__ import annotations

import json
import re

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.contract import is_entry_point, validate
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


def _parse(
    tmp_path, filename: str, src: str, *, capture: bool = True, limit: int = 1000
) -> FileRecord:
    p = tmp_path / filename
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)
    ctx = ParseContext(
        path=filename,
        abs_path=p,
        source=src.encode(),
        repo_root=tmp_path,
        capture_statements=capture,
        statement_text_limit=limit,
    )
    return WsdlParser().parse_file(ctx)


def _tag(s) -> str | None:
    """The XML tag a statement records (`complexType`, `operation`, …) — read from its
    `text`, since `nodeType` is the grammar's one `element` type for every tag."""
    m = re.match(r"<(?:[\w.-]+:)?([\w.-]+)", s.text)
    return m.group(1) if m else None


def _by_name(rec, name: str, tag: str):
    s = next(s for s in rec.statements if s.name == name and _tag(s) == tag)
    assert s.nodeType == "element"
    return s


def test_language_and_extensions() -> None:
    p = WsdlParser()
    assert p.name == "wsdl"
    assert p.extensions == (".wsdl",)
    assert "wsdl" in p.frameworks


def test_message_is_data_model_entity(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    assert rec.language == "xml"  # the format; the WSDL vocabulary is the framework
    assert rec.framework == "wsdl"
    req = _by_name(rec, "GetUserRequest", "message")
    assert req.semanticType == "data_model"
    assert req.parentId == rec.id
    assert req.endpoint == "GetUserRequest"
    assert req.framework == "wsdl"
    assert 'name="id"' in req.text


def test_text_not_truncated_by_statement_text_limit(tmp_path) -> None:
    # Regression: this parser used to slice `node_text(...)[:limit]` itself, which fed the
    # emit-time lossless splitter (emit/split.py) pre-cut text and silently lost the tail.
    # Truncation now happens only at emit time -- the parser must capture the full message
    # regardless of how small `limit` is.
    parts = "".join(f'    <wsdl:part name="p{i}" type="xs:string"/>\n' for i in range(50))
    src = (
        '<?xml version="1.0"?>\n'
        '<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/">\n'
        '  <wsdl:message name="BigRequest">\n' + parts + "  </wsdl:message>\n"
        "</wsdl:definitions>\n"
    )
    rec = _parse(tmp_path, "service.wsdl", src, limit=20)
    big = _by_name(rec, "BigRequest", "message")
    assert 'name="p0"' in big.text
    assert 'name="p49"' in big.text
    assert len(big.text) > 20


def test_text_not_emptied_when_statement_text_limit_is_zero(tmp_path) -> None:
    # `statement_text_limit=0` is documented as "0 disables" -- it must not mean "empty
    # every statement's text", which is what the old `text[:0]` slicing did.
    rec = _parse(tmp_path, "service.wsdl", WSDL, limit=0)
    op = _by_name(rec, "GetUser", "operation")
    assert op.text != ""
    assert 'name="GetUser"' in op.text


def test_operation_is_wiring_not_an_entry_point(tmp_path) -> None:
    # A portType operation names no handler (entry-point contract; docs/entry-point-roles/
    # wsdl.md): it is a plain statement that keeps its SOAP address and DTOs, like a GraphQL
    # SDL root field -- never a `route`, so it is not counted as an entry point.
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    op = _by_name(rec, "GetUser", "operation")
    assert op.semanticType is None
    assert (op.method, op.routeKind, op.isRegex, op.handler) == (None, None, None, None)
    assert op.framework == "wsdl"
    assert op.endpoint == "UserServicePortType/GetUser"
    assert op.requestDTO == "GetUserRequest"
    assert op.responseDTO == "GetUserResponse"


def test_abstract_only_wsdl_without_binding_still_captures_operations(tmp_path) -> None:
    # Regression: real-world WSDLs commonly declare only the abstract interface (portType),
    # with the concrete binding either absent or living in a separate file (the
    # WS-I-recommended split-interface layout). Gating on a same-file binding silently
    # dropped ~1,100 of 2,728 operations in a real-world corpus (Apache CXF). Operations
    # must come directly from portType/operation, with no binding needed at all.
    src = """\
<?xml version="1.0"?>
<wsdl:definitions
    xmlns:tns="http://example.com/x"
    xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/">
  <wsdl:message name="PingRequest"/>
  <wsdl:message name="PingResponse"/>
  <wsdl:portType name="PingPortType">
    <wsdl:operation name="Ping">
      <wsdl:input message="tns:PingRequest"/>
      <wsdl:output message="tns:PingResponse"/>
    </wsdl:operation>
  </wsdl:portType>
</wsdl:definitions>
"""
    rec = _parse(tmp_path, "abstract.wsdl", src)
    op = _by_name(rec, "Ping", "operation")
    assert op.endpoint == "PingPortType/Ping"
    assert op.requestDTO == "PingRequest"
    assert op.responseDTO == "PingResponse"


def test_multiple_bindings_for_same_operation_not_duplicated(tmp_path) -> None:
    # Real-world WSDLs (verified against an ASP.NET-generated example) routinely declare two
    # bindings for the same portType -- a SOAP 1.1 `soap:binding` and a SOAP 1.2
    # `soap12:binding` -- purely for wire-protocol compatibility. Since operations are read
    # directly from portType/operation and bindings are never consulted, this is no longer
    # even a special case -- exactly one record per operation regardless of how many
    # bindings (zero, one, or many) reference its portType.
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
    ops = [s for s in rec.statements if _tag(s) == "operation"]
    assert len(ops) == 1
    assert ops[0].endpoint == "CalculatorSoap/Add"


def test_operations_in_different_port_types_not_merged(tmp_path) -> None:
    # Two portTypes each declare an operation named "Ping" with different messages. Each
    # must produce its own record, scoped by its own portType name in `endpoint` -- never
    # merged or cross-attributed, even though only one of the two has a binding at all.
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
    ops = [s for s in rec.statements if _tag(s) == "operation"]
    assert len(ops) == 2
    by_endpoint = {o.endpoint: o for o in ops}
    assert by_endpoint["PortA/Ping"].requestDTO == "PingARequest"
    assert by_endpoint["PortB/Ping"].requestDTO == "PingBRequest"


def test_port_type_and_operation_missing_name_skipped(tmp_path) -> None:
    # A `portType`/`operation` with no `name=` attribute is malformed WSDL -- skipped, not
    # guessed, same "absent beats wrong" rule as everywhere else in this parser.
    src = """\
<?xml version="1.0"?>
<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/">
  <wsdl:portType>
    <wsdl:operation name="Orphan"/>
  </wsdl:portType>
  <wsdl:portType name="Named">
    <wsdl:operation/>
  </wsdl:portType>
</wsdl:definitions>
"""
    rec = _parse(tmp_path, "service.wsdl", src)
    assert [s for s in rec.statements if _tag(s) == "operation"] == []


def test_binding_only_operation_not_captured(tmp_path) -> None:
    # Negative case (parser-review-guide §5): a <wsdl:operation> living only under a
    # <binding> -- never under a <portType> -- must not be mistaken for an operation
    # declaration. Operations come exclusively from portType; bindings are not modeled.
    src = """\
<?xml version="1.0"?>
<wsdl:definitions
    xmlns:tns="http://example.com/x"
    xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/">
  <wsdl:binding name="OrphanBinding" type="tns:NoSuchPortType">
    <wsdl:operation name="LooksLikeARoute">
      <soap:operation soapAction="urn:looks-like-a-route"/>
    </wsdl:operation>
  </wsdl:binding>
</wsdl:definitions>
"""
    rec = _parse(tmp_path, "service.wsdl", src)
    assert rec.statements == []


def test_embedded_xsd_schema_reuses_xsd_walker(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    user = _by_name(rec, "User", "complexType")
    assert user.semanticType == "data_model"
    # Tagged "wsdl", not "xsd" -- this type is declared inline in the WSDL's own <types>.
    assert user.framework == "wsdl"
    assert user.endpoint == "User"
    assert 'name="id"' in user.text


def test_import_recorded_as_a_statement(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    imp = next(s for s in rec.statements if _tag(s) == "import")
    assert imp.semanticType is None
    assert imp.name == "http://example.com/common"
    assert imp.endpoint is None  # a namespace/location, not a join key
    assert imp.framework == "wsdl"


def test_wsdl_import_and_embedded_schema_locations_resolve(tmp_path) -> None:
    # Both a `wsdl:import location=` and an embedded schema's `schemaLocation=` are resolved
    # against the WSDL file's own folder; URLs are external, missing files are left out.
    for rel in ("svc/common.wsdl", "svc/xsd/types.xsd"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("<x/>")
    src = """\
<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <wsdl:import namespace="urn:common" location="common.wsdl"/>
  <wsdl:import namespace="urn:remote" location="https://example.com/remote?wsdl"/>
  <wsdl:types>
    <xs:schema>
      <xs:include schemaLocation="xsd/types.xsd"/>
      <xs:import namespace="urn:gone" schemaLocation="xsd/missing.xsd"/>
    </xs:schema>
  </wsdl:types>
</wsdl:definitions>
"""
    for capture in (True, False):
        rec = _parse(tmp_path, "svc/service.wsdl", src, capture=capture)
        assert rec.importFiles == ["svc/common.wsdl", "svc/xsd/types.xsd"]
        assert rec.externalImports == ["https://example.com/remote?wsdl"]


def test_soap_address_location_is_not_an_import(tmp_path) -> None:
    # Negative case: `soap:address location=` under service/port is the deployed endpoint
    # URL, not a document this WSDL loads -- it must not become an external import.
    src = """\
<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/">
  <wsdl:service name="S">
    <wsdl:port name="P" binding="tns:B">
      <soap:address location="http://localhost:9000/SoapContext/SoapPort"/>
    </wsdl:port>
  </wsdl:service>
</wsdl:definitions>
"""
    rec = _parse(tmp_path, "service.wsdl", src)
    assert (rec.importFiles, rec.externalImports) == ([], [])


def test_comment_captured(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    comments = [s for s in rec.statements if s.nodeType == "Comment"]
    assert any("shared contract" in s.text for s in comments)


def test_capture_gate(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL, capture=False)
    assert rec.statements == []
    assert rec.language == "xml"
    assert rec.framework is None


def test_fixture_file_captured_like_any_other(tmp_path) -> None:
    # `is_fixture_file` (base.py) gates only entry-point emitters, and a WSDL records no
    # entry points -- a fixture WSDL is captured in full.
    rec = _parse(tmp_path, "service.test.wsdl", WSDL)
    normal = _parse(tmp_path, "service.wsdl", WSDL)
    assert [(_tag(s), s.name) for s in rec.statements] == [
        (_tag(s), s.name) for s in normal.statements
    ]


def test_records_no_entry_points_and_passes_the_contract(tmp_path) -> None:
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    assert not any(is_entry_point(s) for s in rec.statements)
    assert validate(rec) == []


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


def test_node_type_is_the_grammar_type_and_declared(tmp_path) -> None:
    # message, operation, import and embedded-schema records are all the xml grammar's
    # `element` (the tag is in `text`); comments keep `Comment`.
    rec = _parse(tmp_path, "service.wsdl", WSDL)
    kinds = {s.nodeType for s in rec.statements}
    assert kinds == {"element", "Comment"}
    assert {_tag(s) for s in rec.statements if s.nodeType == "element"} >= {
        "message",
        "operation",
        "import",
        "complexType",
    }
    assert kinds - {"Comment"} <= set(WsdlParser().statement_types)


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
