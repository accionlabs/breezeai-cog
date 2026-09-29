"""Walk a ``<definitions>`` element (a standalone ``.wsdl`` document's root) and emit one
flat ``Statement`` per ``message`` declaration plus one ``route`` statement per operation
declared under any ``portType`` — regardless of whether a same-file ``binding`` implements
it.

Real-world WSDLs commonly separate the abstract interface (``portType``) from its concrete
wiring (``binding``) across files (the WS-I-recommended layout), or omit a binding entirely
(an abstract-only interface WSDL). An earlier version of this parser only emitted a route
when a same-file ``binding`` referenced the ``portType``, which silently dropped those
operations — verified against Apache CXF's real corpus: ~1,100 of 2,728 operations had no
same-file binding. ``method`` is always ``"RPC"``, matching every other SOAP/RPC emitter in
this repo (``csharp_wcf``, ASMX, ``dotnet_servicehost``) — a binding's SOAP action is
wire-protocol detail, not the messaging verb the schema's ``method`` field documents, and
there is no dedicated field for it in this v1.

* ``message`` (global) → a ``data_model`` entity, same shape as an XSD global ``element``.
* ``portType/operation`` → a ``route`` statement, the operation's own real grammar node
  (``nodeType="operation"``), ``framework="wsdl"``, ``method="RPC"``, ``routeKind="rpc"``,
  ``endpoint`` = ``f"{portType}/{operation}"``, ``requestDTO``/``responseDTO`` = the
  operation's declared input/output message local names.
* ``import`` → a plain statement recording the referenced namespace/location — never
  resolved (same v1 scope decision as XSD's ``import``/``include``).

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
) -> Statement:
    start, end = line_span(node)
    return Statement(
        id=disambiguate(statement_id(path, start, node.start_point[1]), seen_ids),
        parentId=fid,
        path=path,
        nodeType=tag,
        semanticType=semantic,
        name=name,
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
            out.append(_plain_statement(node, source, path, fid, tag, name, "data_model", seen_ids))
        elif tag == _IMPORT_TAG:
            name = element_attr(node, source, "namespace") or element_attr(node, source, "location")
            out.append(_plain_statement(node, source, path, fid, tag, name, None, seen_ids))
    return out


def _route_statement(
    op: Node, source: bytes, path: str, fid: str, pt_name: str, op_name: str, seen_ids: set[str]
) -> Statement:
    start, end = line_span(op)
    return Statement(
        id=disambiguate(statement_id(path, start, op.start_point[1]), seen_ids),
        parentId=fid,
        path=path,
        nodeType="operation",
        semanticType="route",
        name=op_name,
        text=node_text(op, source),
        framework="wsdl",
        method="RPC",
        endpoint=f"{pt_name}/{op_name}",
        routeKind="rpc",
        isRegex=False,
        requestDTO=_message_local(op, source, "input"),
        responseDTO=_message_local(op, source, "output"),
        startLine=start,
        endLine=end,
    )


def _collect_routes(
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
            out.append(_route_statement(op, source, path, fid, pt_name, op_name, seen_ids))
    return out


def collect_definitions_statements(
    definitions: Node, source: bytes, path: str, seen_ids: set[str]
) -> list[Statement]:
    """Walk a ``<definitions>`` element's direct children and emit ``message``/``route``/
    ``import`` statements, parented to the file."""
    fid = file_id(path)
    return _collect_messages(definitions, source, path, fid, seen_ids) + _collect_routes(
        definitions, source, path, fid, seen_ids
    )
