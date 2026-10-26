"""Standalone XML Schema (XSD) capture.

The ``xsd`` language parser owns ``.xsd`` files. It emits one flat Statement per global
declaration (full body, including nested elements/restrictions, on ``text``,
``endpoint=name`` as the join key, ``framework="xsd"``): ``element``/``complexType`` as a
``data_model`` entity; ``simpleType``/``group``/``attributeGroup`` captured the same way but
with no ``data_model`` tag (reusable fragments / scalar constraints, not domain entities);
``import``/``include`` as a plain statement recording the referenced
namespace/schemaLocation. A relative ``schemaLocation`` that exists in the repo becomes an
``importFiles`` entry (resolved against the schema's folder), a URL an ``externalImports``
entry. Capture is on by default.
"""

from __future__ import annotations

import json
import re

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

  <xs:group name="AddressFields">
    <xs:sequence>
      <xs:element name="city" type="xs:string"/>
    </xs:sequence>
  </xs:group>

  <xs:attributeGroup name="CommonAttrs">
    <xs:attribute name="id" type="xs:ID"/>
  </xs:attributeGroup>

  <xs:element name="Person" type="tns:AddressType"/>
</xs:schema>
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
    return XsdParser().parse_file(ctx)


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
    p = XsdParser()
    assert p.name == "xsd"
    assert p.extensions == (".xsd",)
    assert "xsd" in p.frameworks


def test_complex_type_is_data_model_entity_with_full_body(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    assert rec.language == "xml"  # the format; the XSD vocabulary is the framework
    assert rec.framework == "xsd"
    addr = _by_name(rec, "AddressType", "complexType")
    assert addr.semanticType == "data_model"
    assert addr.parentId == rec.id
    assert addr.endpoint == "AddressType"
    assert addr.framework == "xsd"
    # full body carries nested elements as text (contents capture)
    assert 'name="street"' in addr.text
    assert 'name="zip"' in addr.text


def test_group_and_attribute_group_are_structural_not_entities(tmp_path) -> None:
    # Reusable content fragments (referenced via `ref=` elsewhere), not domain entities --
    # still fully captured (name, endpoint, framework, full body), just no data_model tag.
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    group = _by_name(rec, "AddressFields", "group")
    assert group.semanticType is None
    assert group.endpoint == "AddressFields"
    assert group.framework == "xsd"
    assert 'name="city"' in group.text

    attr_group = _by_name(rec, "CommonAttrs", "attributeGroup")
    assert attr_group.semanticType is None
    assert attr_group.endpoint == "CommonAttrs"
    assert 'name="id"' in attr_group.text


def test_foreign_namespace_attribute_does_not_shadow_real_name(tmp_path) -> None:
    # Regression: `element_attr` used to match by prefix-stripped local name, so a
    # foreign-namespace attribute that merely shares a local name (e.g. an EMF-annotated
    # schema's `ecore:name`) could shadow the real unprefixed `name` if it appeared first in
    # the tag. Attribute prefixes name a genuinely different namespace, unlike element tags.
    src = (
        '<?xml version="1.0"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:ecore="http://x/ecore">\n'
        '  <xs:complexType ecore:name="ProcessingInstructionImpl" name="ProcessingInstruction"/>\n'
        "</xs:schema>\n"
    )
    rec = _parse(tmp_path, "schema.xsd", src)
    names = {s.name for s in rec.statements if _tag(s) == "complexType"}
    assert names == {"ProcessingInstruction"}


def test_text_not_truncated_by_statement_text_limit(tmp_path) -> None:
    # Regression: this parser used to slice `node_text(...)[:limit]` itself, which fed the
    # emit-time lossless splitter (emit/split.py) pre-cut text and silently lost the tail.
    # Truncation now happens only at emit time, on the whole pipeline's statements -- the
    # parser must capture the full declaration regardless of how small `limit` is.
    src = (
        '<?xml version="1.0"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        '  <xs:complexType name="Big">\n'
        "    <xs:sequence>\n"
        + "".join(f'      <xs:element name="f{i}" type="xs:string"/>\n' for i in range(50))
        + "    </xs:sequence>\n"
        "  </xs:complexType>\n"
        "</xs:schema>\n"
    )
    rec = _parse(tmp_path, "schema.xsd", src, limit=20)
    big = _by_name(rec, "Big", "complexType")
    assert 'name="f0"' in big.text
    assert 'name="f49"' in big.text
    assert len(big.text) > 20


def test_text_not_emptied_when_statement_text_limit_is_zero(tmp_path) -> None:
    # `statement_text_limit=0` is documented as "0 disables" -- it must not mean "empty
    # every statement's text", which is what the old `text[:0]` slicing did.
    rec = _parse(tmp_path, "schema.xsd", SCHEMA, limit=0)
    person = _by_name(rec, "Person", "element")
    assert person.text != ""
    assert 'name="Person"' in person.text


def test_simple_type_is_structural_not_an_entity(tmp_path) -> None:
    # Per review decision: a `simpleType` is a scalar constraint (often a reusable one via
    # `base=`), not a standalone domain entity like `element`/`complexType` -- still captured
    # (full body on text, restriction base visible, never a fabricated structured field), but
    # with no `data_model` tag.
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    zip_type = _by_name(rec, "ZipCode", "simpleType")
    assert zip_type.semanticType is None
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
    names = {s.name for s in rec.statements if _tag(s) == "element"}
    assert names == {"Person"}


def test_attribute_value_ending_in_quote_character_preserved(tmp_path) -> None:
    # Regression: `element_attr` used to strip attribute values with `.strip("'\"")`, which
    # removes *every* leading/trailing quote character, not just the delimiter pair -- a
    # `name` that legitimately ends with an apostrophe lost it, corrupting the join key.
    src = (
        '<?xml version="1.0"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        '  <xs:element name="Editors\'"/>\n'
        "</xs:schema>\n"
    )
    rec = _parse(tmp_path, "schema.xsd", src)
    assert any(s.name == "Editors'" for s in rec.statements)


def test_import_recorded_as_a_statement(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    imp = next(s for s in rec.statements if _tag(s) == "import")
    assert imp.semanticType is None
    assert imp.name == "http://example.com/common"
    assert 'schemaLocation="common.xsd"' in imp.text


def test_import_and_include_carry_no_endpoint(tmp_path) -> None:
    # A namespace / schemaLocation is not a name anything joins on, so it must not sit in
    # `endpoint`, where a lookup by entity name could match it.
    src = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        '  <xs:import namespace="urn:common" schemaLocation="common.xsd"/>\n'
        '  <xs:include schemaLocation="types.xsd"/>\n'
        "</xs:schema>\n"
    )
    rec = _parse(tmp_path, "schema.xsd", src)
    refs = [s for s in rec.statements if s.semanticType != "comment"]
    assert [(s.name, s.endpoint, s.framework) for s in refs] == [
        ("urn:common", None, "xsd"),
        ("types.xsd", None, "xsd"),
    ]


def test_non_entity_declaration_keeps_name_as_endpoint(tmp_path) -> None:
    # Same as Prisma's enum/config blocks: a simpleType / group is referenced by name
    # (`type=` / `ref=`), so its name stays the join key even without `data_model`.
    src = (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        '  <xs:simpleType name="Code"><xs:restriction base="xs:string"/></xs:simpleType>\n'
        "</xs:schema>\n"
    )
    rec = _parse(tmp_path, "schema.xsd", src)
    st = next(s for s in rec.statements if s.name == "Code")
    assert (st.semanticType, st.endpoint, st.framework) == (None, "Code", "xsd")


def _schema(*refs: str) -> str:
    return (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        + "".join(f"  {r}\n" for r in refs)
        + "</xs:schema>\n"
    )


def _touch(tmp_path, *rels: str) -> None:
    for rel in rels:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(_schema())


def test_relative_schema_location_resolves_against_the_schema_folder(tmp_path) -> None:
    # A relative schemaLocation is a URI reference resolved against the schema's own folder
    # (W3C XSD), so an existing target becomes an IMPORTS edge -- with or without `./`.
    _touch(tmp_path, "schemas/types.xsd", "schemas/common/base.xsd", "shared/codes.xsd")
    src = _schema(
        '<xs:include schemaLocation="types.xsd"/>',
        '<xs:import namespace="urn:base" schemaLocation="./common/base.xsd"/>',
        '<xs:redefine schemaLocation="../shared/codes.xsd"/>',
    )
    rec = _parse(tmp_path, "schemas/order.xsd", src)
    assert rec.importFiles == [
        "schemas/common/base.xsd",
        "schemas/types.xsd",
        "shared/codes.xsd",
    ]
    assert rec.externalImports == []


def test_absolute_url_schema_location_is_an_external_import(tmp_path) -> None:
    src = _schema('<xs:import namespace="x" schemaLocation="http://www.w3.org/2001/xml.xsd"/>')
    rec = _parse(tmp_path, "order.xsd", src)
    assert rec.importFiles == []
    assert rec.externalImports == ["http://www.w3.org/2001/xml.xsd"]


def test_unresolvable_schema_locations_are_left_out(tmp_path) -> None:
    # Honest-null: a missing file, a path leaving the repo, a root-absolute or backslash
    # path, a self-reference and a namespace-only import never produce an IMPORTS edge.
    (tmp_path.parent / "outside.xsd").write_text(_schema())
    src = _schema(
        '<xs:include schemaLocation="missing.xsd"/>',
        f'<xs:include schemaLocation="../{tmp_path.parent.name}/../outside.xsd"/>',
        '<xs:include schemaLocation="../outside.xsd"/>',
        '<xs:include schemaLocation="/schemas/types.xsd"/>',
        '<xs:include schemaLocation="sub\\types.xsd"/>',
        '<xs:include schemaLocation="order.xsd"/>',
        '<xs:import namespace="urn:only-a-namespace"/>',
    )
    rec = _parse(tmp_path, "order.xsd", src)
    assert (rec.importFiles, rec.externalImports) == ([], [])


def test_symlink_leaving_the_repo_is_not_an_import(tmp_path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.xsd"
    outside.write_text(_schema())
    (tmp_path / "link.xsd").symlink_to(outside)
    rec = _parse(tmp_path, "order.xsd", _schema('<xs:include schemaLocation="link.xsd"/>'))
    assert rec.importFiles == []


def test_percent_encoded_location_and_duplicates(tmp_path) -> None:
    _touch(tmp_path, "my types.xsd")
    src = _schema(
        '<xs:include schemaLocation="my%20types.xsd"/>',
        '<xs:include schemaLocation="my%20types.xsd#frag"/>',
    )
    assert _parse(tmp_path, "order.xsd", src).importFiles == ["my types.xsd"]


def test_imports_resolved_without_statement_capture(tmp_path) -> None:
    # Imports are File-level structure, kept with --no-capture-statements like any language.
    _touch(tmp_path, "types.xsd")
    rec = _parse(
        tmp_path, "order.xsd", _schema('<xs:include schemaLocation="types.xsd"/>'), capture=False
    )
    assert rec.statements == []
    assert rec.importFiles == ["types.xsd"]


def test_non_schema_root_has_no_imports(tmp_path) -> None:
    _touch(tmp_path, "types.xsd")
    src = '<config><include schemaLocation="types.xsd"/></config>\n'
    assert _parse(tmp_path, "order.xsd", src).importFiles == []


def test_comment_captured(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    comments = [s for s in rec.statements if s.nodeType == "Comment"]
    assert any("shared types" in s.text for s in comments)


def test_capture_gate(tmp_path) -> None:
    rec = _parse(tmp_path, "schema.xsd", SCHEMA, capture=False)
    assert rec.statements == []
    assert rec.language == "xml"
    assert rec.framework is None


def test_fixture_file_still_captures_statements(tmp_path) -> None:
    # XSD emits no routes, so `is_fixture_file` (route/entry-point-only, per base.py) must
    # not gate its capture at all.
    rec = _parse(tmp_path, "schema.test.xsd", SCHEMA)
    assert any(_tag(s) == "complexType" for s in rec.statements)


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
    names = {s.name for s in rec.statements if _tag(s) == "complexType"}
    assert names == {"Keep"}
    assert not any(_tag(s) == "annotation" for s in rec.statements)


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


def test_node_type_is_the_grammar_type_and_declared(tmp_path) -> None:
    # nodeType is always the real tree-sitter type: the xml grammar's `element` for every
    # tag (the tag itself is in `text`), `Comment` for comments -- and capabilities lists it.
    rec = _parse(tmp_path, "schema.xsd", SCHEMA)
    kinds = {s.nodeType for s in rec.statements}
    assert kinds == {"element", "Comment"}
    assert kinds - {"Comment"} <= set(XsdParser().statement_types)


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
