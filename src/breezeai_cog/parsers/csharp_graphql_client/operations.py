"""GraphQL.Client (graphql-dotnet) operations: one ``api_call`` per root field the call sends.

::

    private readonly GraphQLHttpClient _client;
    await _client.SendQueryAsync<OrdersResponse>(new GraphQLRequest {
        Query = @"query GetOrders { orders(first: 10) { id } customer { name } }" });
        → api_call  method QUERY  endpoint orders    handler GetOrders  framework graphql
        → api_call  method QUERY  endpoint customer  handler GetOrders  framework graphql

The shape is the TypeScript ``graphql-client`` detector's: the invoked API is each root selection
field (it joins to the server route's ``endpoint``), the operation name is the client-side label
in ``handler``, the first input variable's type is ``requestDTO``, and ``routeKind`` / ``method``
are the operation kind. The operation document is read with the ``graphql`` grammar
(``parsers/graphql/operations.py``).

**Evidence is the receiver's declared type** (``GraphQLHttpClient``, ``IGraphQLClient``,
``IGraphQLWebSocketClient``), never the verb: ``SendQueryAsync`` alone proves nothing.

**The document only from literals:** the ``Query`` of a ``new GraphQLRequest { … }`` /
``new GraphQLRequest(query, …)`` on the call or on a local the call passes, as a string literal
(regular, verbatim or raw), a concatenation of literals, or a ``const`` / ``static readonly``
field of this file holding one. Anything else — or a document with several operations and no
literal ``OperationName`` to pick one — still records the call (``method`` from the client method:
``SendQueryAsync`` is a query) with ``endpoint`` empty.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..csharp.functions import type_map
from ..csharp.statements import collect_typed_names
from ..emits import Emit
from ..graphql.operations import OPERATION_KINDS, operation_request_dto, root_fields
from ..graphql.sdl import child_of_type
from ..treesitter import first_line, node_text, parse_source

_CLIENT_TYPES = {
    "GraphQLHttpClient": "graphql", "IGraphQLClient": "graphql",
    "IGraphQLWebSocketClient": "graphql",
}

#: Client method → the operation kind it sends when the document can't be read.
_METHOD_KINDS = {
    "SendQueryAsync": "query", "SendMutationAsync": "mutation",
    "CreateSubscriptionStream": "subscription",
}

_REQUEST_TYPES = frozenset({"GraphQLRequest", "GraphQLHttpRequest"})

#: Simple escapes in a regular C# string literal.
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "'": "'", "\\": "\\", "0": ""}
_ESCAPE = re.compile(r"\\(.)")


def _iter(root: Node, kind: str) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == kind:
            yield node
        stack.extend(reversed(node.named_children))


def _string(node: Node, source: bytes, consts: dict[str, Node], depth: int = 0) -> str | None:
    """The value of a string expression built only from literals (or this file's string
    constants), else ``None``."""
    if depth > 8:
        return None
    if node.type == "parenthesized_expression" and node.named_children:
        return _string(node.named_children[0], source, consts, depth + 1)
    if node.type == "binary_expression":
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        op = node.child_by_field_name("operator")
        if left is None or right is None or op is None or node_text(op, source) != "+":
            return None
        a = _string(left, source, consts, depth + 1)
        b = _string(right, source, consts, depth + 1) if a is not None else None
        return a + b if a is not None and b is not None else None
    if node.type in ("identifier", "member_access_expression"):
        name = node_text(node, source).rsplit(".", 1)[-1]
        value = consts.get(name)
        return _string(value, source, consts, depth + 1) if value is not None else None
    text = node_text(node, source)
    if node.type == "verbatim_string_literal":
        return text[2:-1].replace('""', '"')
    if node.type == "raw_string_literal":
        quotes = len(text) - len(text.lstrip('"'))
        return text[quotes:-quotes]
    if node.type == "string_literal" and len(text) >= 2:
        return _ESCAPE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(0)), text[1:-1])
    return None


def _string_constants(root: Node, source: bytes) -> dict[str, Node]:
    """``const`` / ``static readonly`` string fields of this file → their initializer; a name
    declared twice is dropped (which one a reference means is not known here)."""
    found: dict[str, Node] = {}
    dropped: set[str] = set()
    for field in _iter(root, "field_declaration"):
        mods = {node_text(m, source) for m in field.named_children if m.type == "modifier"}
        if "const" not in mods and not {"static", "readonly"} <= mods:
            continue
        decl = child_of_type(field, "variable_declaration")
        for d in decl.named_children if decl is not None else []:
            if d.type != "variable_declarator" or len(d.named_children) < 2:
                continue
            name = node_text(d.named_children[0], source)
            if name in found:
                dropped.add(name)
            found[name] = d.named_children[1]
    return {k: v for k, v in found.items() if k not in dropped}


def _receiver_and_method(call: Node, source: bytes) -> tuple[Node, str] | None:
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_access_expression":
        return None
    name, receiver = fn.child_by_field_name("name"), fn.child_by_field_name("expression")
    if name is None or receiver is None:
        return None
    if name.type == "generic_name":
        name = next((c for c in name.named_children if c.type == "identifier"), None)
    return (receiver, node_text(name, source)) if name is not None else None


def _is_client(receiver: Node, source: bytes, clients: dict[str, str]) -> bool:
    if receiver.type == "identifier":
        return node_text(receiver, source) in clients
    if receiver.type == "member_access_expression":
        obj, name = receiver.child_by_field_name("expression"), receiver.child_by_field_name("name")
        return obj is not None and obj.type in ("this", "this_expression") and name is not None \
            and node_text(name, source) in clients
    return False


def _local_creation(name: str, call: Node, source: bytes) -> Node | None:
    """The ``new …`` a local in the call's enclosing method was initialised with, if declared once."""
    scope = call.parent
    while scope is not None and scope.type not in (
        "method_declaration", "constructor_declaration", "local_function_statement",
        "lambda_expression", "accessor_declaration",
    ):
        scope = scope.parent
    if scope is None:
        return None
    hits = [d.named_children[1] for d in _iter(scope, "variable_declarator")
            if len(d.named_children) > 1 and node_text(d.named_children[0], source) == name]
    if len(hits) != 1 or hits[0].type != "object_creation_expression":
        return None
    return hits[0]


def _request(call: Node, source: bytes) -> Node | None:
    """The ``new GraphQLRequest …`` the call sends."""
    args = call.child_by_field_name("arguments")
    first = next((a.named_children[-1] for a in args.named_children if a.named_children), None) \
        if args is not None else None
    if first is None:
        return None
    creation = first if first.type == "object_creation_expression" else (
        _local_creation(node_text(first, source), call, source) if first.type == "identifier"
        else None)
    if creation is None:
        return None
    typ = creation.child_by_field_name("type")
    if typ is None or node_text(typ, source).rsplit(".", 1)[-1] not in _REQUEST_TYPES:
        return None
    return creation


def _query_and_name(creation: Node, source: bytes) -> tuple[Node | None, Node | None]:
    """The ``Query`` and ``OperationName`` expressions of a request: initializer properties, or
    the constructor's first and third arguments (``GraphQLRequest(query, variables, name)``)."""
    query: Node | None = None
    name: Node | None = None
    args = creation.child_by_field_name("arguments")
    positional = [a.named_children[-1] for a in args.named_children if a.named_children] \
        if args is not None else []
    if positional:
        query = positional[0]
        name = positional[2] if len(positional) > 2 else None
    init = child_of_type(creation, "initializer_expression")
    for assign in init.named_children if init is not None else []:
        if assign.type != "assignment_expression":
            continue
        left, right = assign.child_by_field_name("left"), assign.child_by_field_name("right")
        key = node_text(left, source) if left is not None else ""
        if key == "Query":
            query = right
        elif key == "OperationName":
            name = right
    return query, name


def _operation(document: str, wanted: str | None, timeout: int) -> tuple[Node, str, bytes] | None:
    """(operation_definition, kind, document bytes) — the one named ``wanted``, or the only one."""
    body = document.encode()
    gql = parse_source("graphql", body, timeout).root_node
    ops: list[tuple[Node, str, str | None]] = []
    for op in _iter(gql, "operation_definition"):
        ot = child_of_type(op, "operation_type")
        kind = OPERATION_KINDS.get(node_text(ot, body)) if ot is not None else "query"
        nm = child_of_type(op, "name")
        if kind is not None:
            ops.append((op, kind, node_text(nm, body) if nm is not None else None))
    if wanted is not None:
        ops = [o for o in ops if o[2] == wanted]
    if len(ops) != 1:
        return None
    return ops[0][0], ops[0][1], body


def detect_graphql_client_calls(root: Node, source: bytes, timeout: int = 0) -> list[Emit]:
    clients = collect_typed_names(root, source, type_map(root, source), _CLIENT_TYPES)
    if not clients:
        return []
    consts: dict[str, Node] | None = None
    found: list[Emit] = []
    for call in _iter(root, "invocation_expression"):
        parts = _receiver_and_method(call, source)
        if parts is None or parts[1] not in _METHOD_KINDS or not _is_client(parts[0], source, clients):
            continue
        text = first_line(node_text(call, source))[:120]
        resolved = None
        creation = _request(call, source)
        if creation is not None:
            if consts is None:
                consts = _string_constants(root, source)
            query, name = _query_and_name(creation, source)
            document = _string(query, source, consts) if query is not None else None
            wanted = _string(name, source, consts) if name is not None else None
            if document is not None and (name is None or wanted is not None):
                resolved = _operation(document, wanted, timeout)
        if resolved is None:
            kind = _METHOD_KINDS[parts[1]]
            found.append(Emit(
                call, {"semanticType": "api_call", "method": kind.upper(), "framework": "graphql",
                       "routeKind": kind},
                enrich="unclassified", text=text,
            ))
            continue
        op, kind, body = resolved
        op_name_node = child_of_type(op, "name")
        op_name = node_text(op_name_node, body) if op_name_node is not None else None
        request_dto = operation_request_dto(op, body)
        for field in root_fields(op):
            name_node = child_of_type(field, "name")
            if name_node is None:
                continue
            found.append(Emit(
                call,
                {"semanticType": "api_call", "method": kind.upper(), "framework": "graphql",
                 "routeKind": kind, "endpoint": node_text(name_node, body)},
                fill={"handler": op_name, "requestDTO": request_dto},
                enrich="unclassified", text=text,
            ))
    return found


def _detect(dc: DetectContext) -> Detection | None:
    found = detect_graphql_client_calls(dc.root, dc.source, dc.ctx.parse_timeout_micros)
    return Detection(found) if found else None


# Outbound calls; mock / fixture files are skipped like the TypeScript client detector.
register_detector(Detector(
    name="graphql-client", gate=Gate("csharp", order=80, guard=b"GraphQL", skip_fixtures=True),
    detect=_detect, frameworks=("graphql",),
))
