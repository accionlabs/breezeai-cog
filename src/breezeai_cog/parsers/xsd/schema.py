"""Walk a ``<schema>`` element (a standalone ``.xsd`` document's root, or the
``<xsd:schema>`` embedded in a WSDL's ``<wsdl:types>``, reused by ``wsdl/operations.py``)
and emit one flat ``Statement`` per **global** declaration — the same "top-level only, full
body on ``text``" philosophy as ``prisma/schema.py``: nested/local element and attribute
declarations inside a ``complexType``/``simpleType`` stay in that type's ``text``, never
split into their own records.

* ``element`` / ``complexType`` (global) → a ``data_model`` entity (parallels a Prisma
  ``model``), the full declaration on ``text``, ``endpoint=name`` (the join key — matches
  Prisma's own ``data_model`` statements, so a WSDL operation's ``requestDTO``/``responseDTO``
  can resolve to the ``message``/type it names) and ``framework`` set to the caller's
  ``framework`` (``"xsd"`` for a standalone ``.xsd`` file, ``"wsdl"`` when this walker is
  reused for a schema embedded in a WSDL's ``<wsdl:types>``).
* ``simpleType`` / ``group`` / ``attributeGroup`` (global) → captured the same way, but with
  ``semanticType=None`` — these are reusable content fragments / scalar constraints (referenced
  via ``ref=``/``base=`` elsewhere), not standalone domain entities, so they don't carry the
  ``data_model`` tag.
* ``import`` / ``include`` → a plain statement recording the referenced namespace /
  schemaLocation, with no ``endpoint``. The documents themselves (:func:`schema_locations`,
  also ``redefine``/``override``) become the file's ``importFiles`` / ``externalImports``,
  resolved by ``xml_common.resolve_locations``: a relative ``schemaLocation`` is resolved against the
  schema's own folder (the W3C rule) and kept only when that file is in the repo; a URL is an
  external import.

Anything else at schema scope (``annotation``, ``notation``, ``redefine``, ``override``,
top-level ``key``/``unique``) is intentionally skipped — not a declaration this parser
models (absent beats wrong, same rule ``prisma/schema.py`` applies to its own grammar gaps).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import SemanticType, Statement
from ..treesitter import line_span, node_text
from ..xml_common import child_elements, element_attr, element_tag

#: Global declarations this walker captures structurally (full body on `text`).
_STRUCTURAL_TAGS = ("element", "complexType", "simpleType", "group", "attributeGroup")
#: Of those, the ones that are real domain entities (vs. a reusable fragment / constraint)
#: and so carry `semanticType="data_model"`.
_ENTITY_TAGS = ("element", "complexType")
_IMPORT_TAGS = ("import", "include")


#: Schema-composition elements whose ``schemaLocation`` names another schema document.
_LOCATION_TAGS = ("include", "import", "redefine", "override")


def schema_locations(schema_node: Node, source: bytes) -> list[str]:
    """The ``schemaLocation`` of every top-level ``include``/``import``/``redefine``/
    ``override`` — the documents this schema loads (an ``import`` naming only a namespace
    loads no document, so it contributes nothing)."""
    out: list[str] = []
    for node in child_elements(schema_node):
        if element_tag(node, source) in _LOCATION_TAGS:
            loc = element_attr(node, source, "schemaLocation")
            if loc:
                out.append(loc)
    return out


def collect_schema_statements(
    schema_node: Node, source: bytes, path: str, seen_ids: set[str], framework: str
) -> list[Statement]:
    """Walk a ``<schema>`` element's direct children and emit one flat ``Statement`` per
    global declaration, parented to the file. ``framework`` is the owning parser's label
    (``"xsd"`` or, when reused for an embedded schema, ``"wsdl"``)."""
    fid = file_id(path)
    out: list[Statement] = []

    for node in child_elements(schema_node):
        tag = element_tag(node, source)
        if tag not in _STRUCTURAL_TAGS and tag not in _IMPORT_TAGS:
            continue  # annotation/notation/redefine/override/… — not modeled

        start, end = line_span(node)
        name = element_attr(node, source, "name")
        semantic: SemanticType | None = "data_model" if tag in _ENTITY_TAGS else None
        if tag in _IMPORT_TAGS:
            semantic = None
            name = element_attr(node, source, "namespace") or element_attr(
                node, source, "schemaLocation"
            )

        out.append(
            Statement(
                id=disambiguate(statement_id(path, start, node.start_point[1]), seen_ids),
                parentId=fid,
                path=path,
                nodeType=node.type,  # the grammar's `element`; the tag is read from `text`
                semanticType=semantic,
                name=name,
                # A declaration's name is its join key (a `type=`/`ref=` elsewhere names it),
                # same as Prisma. An import/include's "name" is a namespace or file location
                # — not a name anything joins on — so it gets no endpoint.
                endpoint=None if tag in _IMPORT_TAGS else name,
                framework=framework,
                text=node_text(node, source),
                startLine=start,
                endLine=end,
            )
        )
    return out
