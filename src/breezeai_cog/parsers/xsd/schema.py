"""Walk a ``<schema>`` element (a standalone ``.xsd`` document's root, or the
``<xsd:schema>`` embedded in a WSDL's ``<wsdl:types>``, reused by ``wsdl/operations.py``)
and emit one flat ``Statement`` per **global** declaration — the same "top-level only, full
body on ``text``" philosophy as ``prisma/schema.py``: nested/local element and attribute
declarations inside a ``complexType``/``simpleType`` stay in that type's ``text``, never
split into their own records.

* ``element`` / ``complexType`` / ``simpleType`` / ``group`` / ``attributeGroup`` (global)
  → a ``data_model`` entity (parallels a Prisma ``model``), the full declaration on ``text``.
* ``import`` / ``include`` → a plain statement recording the referenced namespace /
  schemaLocation — never resolved across files (honest-null; matches this project's v1 scope
  decision to record cross-file XSD/WSDL references without resolving them).

Anything else at schema scope (``annotation``, ``notation``, ``redefine``, ``override``,
top-level ``key``/``unique``) is intentionally skipped — not a declaration this parser
models (absent beats wrong, same rule ``prisma/schema.py`` applies to its own grammar gaps).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement
from ..treesitter import line_span, node_text
from ..xml_common import child_elements, element_attr, element_tag

_ENTITY_TAGS = ("element", "complexType", "simpleType", "group", "attributeGroup")
_IMPORT_TAGS = ("import", "include")


def collect_schema_statements(
    schema_node: Node, source: bytes, path: str, seen_ids: set[str], limit: int
) -> list[Statement]:
    """Walk a ``<schema>`` element's direct children and emit one flat ``Statement`` per
    global declaration, parented to the file."""
    fid = file_id(path)
    out: list[Statement] = []

    for node in child_elements(schema_node):
        tag = element_tag(node, source)
        if tag not in _ENTITY_TAGS and tag not in _IMPORT_TAGS:
            continue  # annotation/notation/redefine/override/… — not modeled

        start, end = line_span(node)
        name = element_attr(node, source, "name")
        if tag in _IMPORT_TAGS:
            name = (
                element_attr(node, source, "namespace")
                or element_attr(node, source, "schemaLocation")
            )

        out.append(
            Statement(
                id=disambiguate(statement_id(path, start, node.start_point[1]), seen_ids),
                parentId=fid,
                path=path,
                nodeType=tag,
                semanticType="data_model" if tag in _ENTITY_TAGS else None,
                name=name,
                text=node_text(node, source)[:limit],
                startLine=start,
                endLine=end,
            )
        )
    return out
