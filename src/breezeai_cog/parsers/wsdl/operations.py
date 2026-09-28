"""Walk a ``<definitions>`` element (a standalone ``.wsdl`` document's root) and emit one
flat ``Statement`` per ``message`` declaration plus one merged ``route`` statement per
operation — correlating ``portType/operation`` (the operation's declared input/output
messages) with ``binding/operation`` (the SOAP action actually wired to it) via the
binding's own ``type=`` attribute, which references a *specific* ``portType`` by QName. A
bare operation-name match across the whole document would wrongly merge same-named
operations declared under two different ``portType``s; this two-hop correlation avoids that.

* ``message`` (global) → a ``data_model`` entity, same shape as an XSD global ``element``.
* ``portType/operation`` + ``binding/operation`` (merged) → a ``route`` statement,
  ``nodeType="synthetic"`` (this record isn't one literal grammar node — same reasoning
  ``csharp_wcf/routes.py`` documents for its own two-source-merged SOAP route),
  ``framework="wsdl"``, ``method`` = the ``soap:operation soapAction=`` value when the
  binding declares one, else ``"RPC"`` (the same fallback ``csharp_wcf``/ASMX detection
  uses), ``routeKind="rpc"``, ``endpoint`` = ``f"{portType}/{operation}"``,
  ``requestDTO``/``responseDTO`` = the operation's declared input/output message local names.
* ``import`` → a plain statement recording the referenced namespace/location — never
  resolved (same v1 scope decision as XSD's ``import``/``include``).

A ``binding/operation`` with no matching ``portType/operation`` (malformed WSDL) is skipped,
not guessed — same "absent beats wrong" rule ``prisma/schema.py`` applies to its own
grammar gaps.

Real-world WSDLs (verified against an ASP.NET-generated example) routinely declare **two**
bindings for the same ``portType`` — one ``soap:binding`` (SOAP 1.1) and one
``soap12:binding`` (SOAP 1.2), purely for wire-protocol compatibility, not two distinct
operations. Without deduplication this doubles every operation. Each ``(portType,
operation)`` pair is therefore emitted **once**, from the first binding that implements it
(SOAP 1.1 conventionally precedes SOAP 1.2 in these files) — a second binding's differing
``soapAction`` is not separately captured in this v1.
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
    limit: int,
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
        text=node_text(node, source)[:limit],
        startLine=start,
        endLine=end,
    )


def _named_children(parent: Node, source: bytes, tag: str) -> dict[str, Node]:
    """Direct child elements of ``parent`` whose local tag == ``tag``, keyed by their
    declared ``name`` (children with no ``name`` attribute are skipped)."""
    out: dict[str, Node] = {}
    for child in child_elements(parent):
        if element_tag(child, source) != tag:
            continue
        name = element_attr(child, source, "name")
        if name:
            out[name] = child
    return out


def _message_local(op: Node, source: bytes, tag: str) -> str | None:
    """The local (prefix-stripped) ``message=`` reference on an operation's ``<input>``/
    ``<output>`` child, e.g. ``tns:GetUserRequest`` → ``GetUserRequest``."""
    child = next((c for c in child_elements(op) if element_tag(c, source) == tag), None)
    if child is None:
        return None
    ref = element_attr(child, source, "message")
    return local_name(ref) if ref else None


def _soap_action(binding_op: Node, source: bytes) -> str | None:
    """The ``soapAction=`` value of the ``soap:operation``/``soap12:operation`` nested
    directly under a binding's ``operation`` (both reduce to local tag ``operation`` — the
    grammar is namespace-unaware), if present."""
    soap_op = next(
        (c for c in child_elements(binding_op) if element_tag(c, source) == "operation"), None
    )
    return element_attr(soap_op, source, "soapAction") if soap_op is not None else None


def collect_definitions_statements(
    definitions: Node, source: bytes, path: str, seen_ids: set[str], limit: int
) -> list[Statement]:
    """Walk a ``<definitions>`` element's direct children and emit ``message``/``route``/
    ``import`` statements, parented to the file."""
    fid = file_id(path)
    out: list[Statement] = []

    for node in child_elements(definitions):
        tag = element_tag(node, source)
        if tag == _MESSAGE_TAG:
            name = element_attr(node, source, "name")
            out.append(
                _plain_statement(node, source, path, fid, tag, name, "data_model", limit, seen_ids)
            )
        elif tag == _IMPORT_TAG:
            name = element_attr(node, source, "namespace") or element_attr(node, source, "location")
            out.append(_plain_statement(node, source, path, fid, tag, name, None, limit, seen_ids))

    port_types = _named_children(definitions, source, "portType")
    emitted: set[tuple[str, str]] = set()

    for binding in child_elements(definitions):
        if element_tag(binding, source) != "binding":
            continue
        type_ref = element_attr(binding, source, "type")
        if type_ref is None:
            continue
        pt_name = local_name(type_ref)
        port_type = port_types.get(pt_name)
        if port_type is None:
            continue
        operations = _named_children(port_type, source, "operation")

        for binding_op in child_elements(binding):
            if element_tag(binding_op, source) != "operation":
                continue
            op_name = element_attr(binding_op, source, "name")
            if not op_name:
                continue
            port_op = operations.get(op_name)
            if port_op is None:
                continue  # binding references an operation the portType never declared
            if (pt_name, op_name) in emitted:
                continue  # a second binding (e.g. SOAP 1.2) for the same operation
            emitted.add((pt_name, op_name))

            start, end = line_span(port_op)
            out.append(
                Statement(
                    id=disambiguate(statement_id(path, start, port_op.start_point[1]), seen_ids),
                    parentId=fid,
                    path=path,
                    nodeType="synthetic",
                    semanticType="route",
                    name=op_name,
                    text=node_text(port_op, source)[:limit],
                    framework="wsdl",
                    method=_soap_action(binding_op, source) or "RPC",
                    endpoint=f"{pt_name}/{op_name}",
                    routeKind="rpc",
                    requestDTO=_message_local(port_op, source, "input"),
                    responseDTO=_message_local(port_op, source, "output"),
                    startLine=start,
                    endLine=end,
                )
            )
    return out
