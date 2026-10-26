"""Walk a ``<definitions>`` element (a standalone ``.wsdl`` document's root) and emit one
flat ``Statement`` per ``message`` declaration plus one per operation declared under any
``portType`` — regardless of whether a same-file ``binding`` implements it.

A ``portType`` operation is **wiring, not an entry point** (entry-point contract,
``docs/entry-point-roles/wsdl.md``): it declares an operation's name and messages but names
no handler — the handler is the implementing code (a WCF ``[OperationContract]``, an ASMX
``[WebMethod]``), which is the binding. This is the same rule as a GraphQL SDL root field.
Recording it as a ``route`` would count one operation twice next to its implementation, and
would turn the WSDL of a remote service the code only *calls* into an inbound entry point.

Real-world WSDLs commonly separate the abstract interface (``portType``) from its concrete
wiring (``binding``) across files (the WS-I-recommended layout), or omit a binding entirely
(an abstract-only interface WSDL), so operations are read from the ``portType`` itself.

* ``message`` (global) → a ``data_model`` entity, same shape as an XSD global ``element``:
  ``framework="wsdl"``, ``endpoint=name`` (the join key — matches Prisma's own ``data_model``
  statements, so an operation's ``requestDTO``/``responseDTO`` can resolve to the ``message`` it
  names).
* ``portType/operation`` → a plain statement (no ``semanticType``), ``framework="wsdl"``,
  ``endpoint`` = ``f"{portType}/{operation}"`` (the SOAP address, kept so it is recorded
  somewhere even when the implementation isn't in the repo), ``requestDTO``/``responseDTO``
  = the operation's declared input/output message local names — the DTOs a GraphQL SDL
  root field also keeps.
* ``import`` → a plain statement recording the referenced namespace/location, with no
  ``endpoint`` — never resolved to a file, for the same reason as XSD's ``import``/
  ``include`` (the location is often a URL or not in the repo).

``binding``/``service``/``port`` elements are not modeled — not a declaration this parser
captures (absent beats wrong, same rule ``prisma/schema.py`` applies to its own grammar
gaps).
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, file_id, statement_id
from ...schemas import SemanticType, Statement
from ..treesitter import line_span, node_text
from ..xml_common import child_elements, element_attr, element_tag, local_name

_MESSAGE_TAG = "message"
_IMPORT_TAG = "import"


def _plain_statement(
    node: Node,
    source: bytes,
    path: str,
    fid: str,
    tag: str,
    name: str | None,
    semantic: SemanticType | None,
    seen_ids: set[str],
    *,
    endpoint: str | None,
) -> Statement:
    start, end = line_span(node)
    return Statement(
        id=disambiguate(statement_id(path, start, node.start_point[1]), seen_ids),
        parentId=fid,
        path=path,
        nodeType=node.type,  # the grammar's `element`; the tag is read from `text`
        semanticType=semantic,
        name=name,
        endpoint=endpoint,
        framework="wsdl",
        text=node_text(node, source),
        startLine=start,
        endLine=end,
    )


def _message_local(op: Node, source: bytes, tag: str) -> str | None:
    """The local (prefix-stripped) ``message=`` reference on an operation's ``<input>``/
    ``<output>`` child, e.g. ``tns:GetUserRequest`` → ``GetUserRequest``."""
    child = next((c for c in child_elements(op) if element_tag(c, source) == tag), None)
    if child is None:
        return None
    ref = element_attr(child, source, "message")
    return local_name(ref) if ref else None


def _collect_messages(
    definitions: Node, source: bytes, path: str, fid: str, seen_ids: set[str]
) -> list[Statement]:
    out: list[Statement] = []
    for node in child_elements(definitions):
        tag = element_tag(node, source)
        if tag == _MESSAGE_TAG:
            name = element_attr(node, source, "name")
            out.append(
                _plain_statement(
                    node, source, path, fid, tag, name, "data_model", seen_ids, endpoint=name
                )
            )
        elif tag == _IMPORT_TAG:
            # The namespace/location is not a name anything joins on: no endpoint.
            name = element_attr(node, source, "namespace") or element_attr(node, source, "location")
            out.append(
                _plain_statement(node, source, path, fid, tag, name, None, seen_ids, endpoint=None)
            )
    return out


def _operation_statement(
    op: Node, source: bytes, path: str, fid: str, pt_name: str, op_name: str, seen_ids: set[str]
) -> Statement:
    start, end = line_span(op)
    return Statement(
        id=disambiguate(statement_id(path, start, op.start_point[1]), seen_ids),
        parentId=fid,
        path=path,
        nodeType=op.type,
        name=op_name,
        text=node_text(op, source),
        framework="wsdl",
        endpoint=f"{pt_name}/{op_name}",
        requestDTO=_message_local(op, source, "input"),
        responseDTO=_message_local(op, source, "output"),
        startLine=start,
        endLine=end,
    )


def _collect_operations(
    definitions: Node, source: bytes, path: str, fid: str, seen_ids: set[str]
) -> list[Statement]:
    out: list[Statement] = []
    for port_type in child_elements(definitions):
        if element_tag(port_type, source) != "portType":
            continue
        pt_name = element_attr(port_type, source, "name")
        if not pt_name:
            continue
        for op in child_elements(port_type):
            if element_tag(op, source) != "operation":
                continue
            op_name = element_attr(op, source, "name")
            if not op_name:
                continue
            out.append(_operation_statement(op, source, path, fid, pt_name, op_name, seen_ids))
    return out


def collect_definitions_statements(
    definitions: Node, source: bytes, path: str, seen_ids: set[str]
) -> list[Statement]:
    """Walk a ``<definitions>`` element's direct children and emit ``message``/``import``/
    ``portType`` operation statements, parented to the file."""
    fid = file_id(path)
    return _collect_messages(definitions, source, path, fid, seen_ids) + _collect_operations(
        definitions, source, path, fid, seen_ids
    )
