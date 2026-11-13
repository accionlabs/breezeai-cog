"""GraphQL.Client (graphql-dotnet) operations in VB: one ``api_call`` per root field the call sends
— the C# detector's rules (``csharp_graphql_client/operations.py``, shared tables in
``graphql_dotnet_common``), read from the VB tree.

::

    Private ReadOnly _gql As GraphQLHttpClient
    Await _gql.SendQueryAsync(Of OrdersResponse)(New GraphQLRequest With {
        .Query = "query GetOrders { orders { id } }"})
        → api_call  method QUERY  endpoint orders  handler GetOrders  framework graphql

**Evidence is the receiver's declared type** (``GraphQLHttpClient``, ``IGraphQLClient``,
``IGraphQLWebSocketClient``; ``vb/scopes.py``), never the verb. **The document only from string
literals** (``"…"``, ``"…" & "…"``): the ``.Query`` of a ``New GraphQLRequest With { … }`` /
``New GraphQLRequest(query, …)`` on the call or on a local the call passes. Anything else — or a
document with several operations and no literal ``.OperationName`` to pick one — still records
the call (``method`` from the client method) with ``endpoint`` empty.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..graphql.operations import operation_request_dto, root_fields
from ..graphql.sdl import child_of_type
from ..graphql_dotnet_common import CLIENT_TYPES, METHOD_KINDS, REQUEST_TYPES, pick_operation
from ..treesitter import first_line, node_text
from ..vb.scopes import (
    Call, bare, initializer_pairs, iter_calls, local_creation, receiver_type, simple_type,
    string_value,
)

_CLIENTS = frozenset(t.lower() for t in CLIENT_TYPES)
_REQUESTS = frozenset(t.lower() for t in REQUEST_TYPES)
_KINDS = {m.lower(): k for m, k in METHOD_KINDS.items()}


def _request(call: Call, source: bytes) -> Node | None:
    """The ``New GraphQLRequest …`` the call sends."""
    if not call.args:
        return None
    first = call.args[0]
    creation = first if first.type == "new_expression" else (
        local_creation(first, source) if first.type == "identifier" else None)
    if creation is None:
        return None
    typ = creation.child_by_field_name("type")
    name = simple_type(node_text(typ, source)) if typ is not None else None
    return creation if name is not None and name.lower() in _REQUESTS else None


def _query_and_name(creation: Node, source: bytes) -> tuple[Node | None, Node | None]:
    """The ``Query`` and ``OperationName`` of a request: ``With`` members, or the constructor's
    first and third arguments (``New GraphQLRequest(query, variables, name)``)."""
    query: Node | None = None
    name: Node | None = None
    args = next((c for c in creation.named_children if c.type == "argument_list"), None)
    positional = [bare(a) for a in args.named_children if a.type == "argument"] \
        if args is not None else []
    if positional:
        query = positional[0]
        name = positional[2] if len(positional) > 2 else None
    for key, value in initializer_pairs(creation, source):
        if key.lower() == "query":
            query = value
        elif key.lower() == "operationname":
            name = value
    return query, name


def detect_graphql_client_calls(root: Node, source: bytes, timeout: int = 0) -> list[Emit]:
    found: list[Emit] = []
    for call in iter_calls(root, source):
        kind = _KINDS.get(call.method.lower())
        if kind is None:
            continue
        typ = receiver_type(call.receiver, source)
        if typ is None or typ.lower() not in _CLIENTS:
            continue
        text = first_line(node_text(call.node, source))[:120]
        resolved = None
        creation = _request(call, source)
        if creation is not None:
            query, name = _query_and_name(creation, source)
            document = string_value(query, source) if query is not None else None
            wanted = string_value(name, source) if name is not None else None
            if document is not None and (name is None or wanted is not None):
                resolved = pick_operation(document, wanted, timeout)
        if resolved is None:
            found.append(Emit(
                call.node, {"semanticType": "api_call", "method": kind.upper(),
                            "framework": "graphql", "routeKind": kind},
                enrich="unclassified", text=text,
            ))
            continue
        op, op_kind, body, fragments = resolved
        op_name_node = child_of_type(op, "name")
        op_name = node_text(op_name_node, body) if op_name_node is not None else None
        request_dto = operation_request_dto(op, body)
        names: list[str | None] = [node_text(n, body) for _, f in root_fields(op, fragments)
                                   if (n := child_of_type(f, "name")) is not None]
        for endpoint in names or [None]:
            found.append(Emit(
                call.node,
                {"semanticType": "api_call", "method": op_kind.upper(), "framework": "graphql",
                 "routeKind": op_kind, "endpoint": endpoint},
                fill={"handler": op_name, "requestDTO": request_dto},
                enrich="unclassified", text=text,
            ))
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = detect_graphql_client_calls(dc.root, dc.source, dc.ctx.parse_timeout_micros)
    return Detection(found) if found else None


register_detector(Detector(
    name="graphql-client", gate=Gate("vb", order=20, guard=b"GraphQL", skip_fixtures=True),
    detect=_detect, frameworks=("graphql",),
))
