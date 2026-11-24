"""GraphQL.Client (graphql-dotnet) rules shared by the C# and VB detectors: the client and request
types, the operation kind each send method implies, and picking the operation a request names
from its document (read with the ``graphql`` grammar)."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from .graphql.operations import OPERATION_KINDS, fragment_definitions
from .graphql.sdl import child_of_type
from .treesitter import node_text, parse_source

CLIENT_TYPES = {
    "GraphQLHttpClient": "graphql", "IGraphQLClient": "graphql",
    "IGraphQLWebSocketClient": "graphql",
}

#: Client method → the operation kind it sends when the document can't be read.
METHOD_KINDS = {
    "SendQueryAsync": "query", "SendMutationAsync": "mutation",
    "CreateSubscriptionStream": "subscription",
}

REQUEST_TYPES = frozenset({"GraphQLRequest", "GraphQLHttpRequest"})


def _iter(root: Node, kind: str) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == kind:
            yield node
        stack.extend(reversed(node.named_children))


def pick_operation(document: str, wanted: str | None,
                   timeout: int) -> tuple[Node, str, bytes, dict[str, Node]] | None:
    """(operation_definition, kind, document bytes, its fragments) — the one named ``wanted``, or
    the only one."""
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
    return ops[0][0], ops[0][1], body, fragment_definitions(gql)
