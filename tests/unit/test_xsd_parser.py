"""Standalone XML Schema (XSD) capture.

The ``xsd`` language parser owns ``.xsd`` files. It emits one flat Statement per global
declaration — ``complexType``/``simpleType``/``group``/``attributeGroup``/``element`` as a
``data_model`` entity (full declaration, including nested elements/restrictions, on
``text``), ``import``/``include`` as a plain statement recording the referenced
namespace/schemaLocation (never resolved across files) — each carrying its declared
``name``. Capture is gated on ``--capture-statements``.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.core import registry
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.xsd.parser import XsdParser
from breezeai_cog.schemas import FileRecord

SCHEMA = """\
<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           targetNamespace="http://example.com/ns"
           xmlns:tns="http://example.com/ns">
  <!-- shared types -->
  <xs:import namespace="http://example.com/common" schemaLocation="common.xsd"/>

  <xs:simpleType name="ZipCode">
    <xs:restriction base="xs:string">
      <xs:pattern value="[0-9]{5}"/>
    </xs:restriction>
  </xs:simpleType>

  <xs:complexType name="AddressType">
    <xs:sequence>
      <xs:element name="street" type="xs:string"/>
      <xs:element name="zip" type="tns:ZipCode"/>
    </xs:sequence>
  </xs:complexType>

  <xs:element name="Person" type="tns:AddressType"/>
</xs:schema>
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
    return XsdParser().parse_file(ctx)


def _by_name(rec, name: str, node_type: str):
    return next(s for s in rec.statements if s.name == name and s.nodeType == node_type)


def test_language_and_extensions() -> None:
    p = XsdParser()
    assert p.name == "xsd"
    assert p.extensions == (".xsd",)
    assert "xsd" in p.frameworks


def test_complex_type_is_data_model_entity_with_full_body(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    assert rec.language == "xsd"
    assert rec.framework == "xsd"
    addr = _by_name(rec, "AddressType", "complexType")
    assert addr.semanticType == "data_model"
    assert addr.parentId == rec.id
    # full body carries nested elements as text (contents capture)
    assert 'name="street"' in addr.text
    assert 'name="zip"' in addr.text


def test_simple_type_restriction_carried_on_text_not_as_edge(tmp_path) -> None:
    # A restriction base is honest-null: it stays visible in the type's text, but the
    # parser never fabricates a structured extends/base field.
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    zip_type = _by_name(rec, "ZipCode", "simpleType")
    assert zip_type.semanticType == "data_model"
    assert 'base="xs:string"' in zip_type.text


def test_global_element_is_data_model_entity(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    person = _by_name(rec, "Person", "element")
    assert person.semanticType == "data_model"
    assert 'type="tns:AddressType"' in person.text


def test_nested_elements_not_split_into_own_records(tmp_path) -> None:
    # `street`/`zip` are local (nested) elements inside AddressType — not global, so they
    # must not appear as their own top-level Statement records.
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    names = {s.name for s in rec.statements if s.nodeType == "element"}
    assert names == {"Person"}


def test_import_recorded_not_resolved(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    imp = next(s for s in rec.statements if s.nodeType == "import")
    assert imp.semanticType is None
    assert imp.name == "http://example.com/common"
    assert 'schemaLocation="common.xsd"' in imp.text


def test_comment_captured(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    comments = [s for s in rec.statements if s.nodeType == "Comment"]
    assert any("shared types" in s.text for s in comments)


def test_capture_gate(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA, capture=False)
    assert rec.statements == []
    assert rec.language == "xsd"
    assert rec.framework is None


def test_annotation_and_other_unmodeled_tags_skipped(tmp_path) -> None:
    # `annotation` (and similarly notation/redefine/override) is real, well-formed XSD but
    # not a declaration this parser models — skipped, not guessed; its sibling is untouched.
    src = (
        '<?xml version="1.0"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        "  <xs:annotation><xs:documentation>doc</xs:documentation></xs:annotation>\n"
        '  <xs:complexType name="Keep">\n'
        '    <xs:sequence><xs:element name="a" type="xs:string"/></xs:sequence>\n'
        "  </xs:complexType>\n"
        "</xs:schema>\n"
    )
    rec = _parse(tmp_path, "schema.xsd", src)
    names = {s.name for s in rec.statements if s.nodeType == "complexType"}
    assert names == {"Keep"}
    assert not any(s.nodeType == "annotation" for s in rec.statements)


def test_malformed_markup_yields_no_statements_not_a_crash(tmp_path) -> None:
    # An unclosed tag makes the whole document fail to reduce to a valid `<schema>` root
    # (verified empirically: this grammar's error recovery is document-wide, not localized
    # per-block like Prisma's) — the parser must not crash or guess partial structure; it
    # returns a valid, empty-statement FileRecord instead.
    src = (
        '<?xml version="1.0"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        '  <xs:complexType name="Broken">\n'
        "</xs:schema>\n"
    )
    rec = _parse(tmp_path, "schema.xsd", src)
    assert rec.statements == []


def test_non_schema_root_yields_no_statements(tmp_path) -> None:
    # A `.xsd`-named file whose root isn't `<schema>` (e.g. a stray XML fragment) is parsed
    # without error but yields no declarations — never guessed.
    rec = _parse(tmp_path, "not-a-schema.xsd", "<root><a/></root>\n")
    assert rec.statements == []


def test_records_validate_against_schema(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    validator = Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
    errors = list(validator.iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
    assert rec.statements  # non-empty


def test_registered_and_selected_by_extension() -> None:
    registry.clear()
    registry.discover_builtin()
    sel = registry.select("schema.xsd", SCHEMA.encode())
    assert sel is not None and sel.name == "xsd"
    assert ".xsd" in registry.capabilities()["extensions"]
    assert "xsd" in registry.capabilities()["languages"]
    registry.clear()
