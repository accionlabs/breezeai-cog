"""Reading a GraphQL operation document (``query GetOrders { orders { id } }``) — what a client
sends. Shared by every language's GraphQL client detector: the invoked API is each root
selection field, which joins to the server route's ``endpoint``."""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text
from .sdl import child_of_type, type_text

#: ``operation_type`` keyword → ``routeKind``.
OPERATION_KINDS = {"query": "query", "mutation": "mutation", "subscription": "subscription"}


def root_fields(op_def: Node) -> list[Node]:
    """Root selection-set fields of an operation — the actual API operations invoked.
    Skips bare fragment spreads (``...Foo``) at the root, which carry no field name."""
    sel_set = child_of_type(op_def, "selection_set")
    if sel_set is None:
        return []
    fields: list[Node] = []
    for selection in sel_set.named_children:
        if selection.type != "selection":
            continue
        field = child_of_type(selection, "field")
        if field is not None and child_of_type(field, "name") is not None:
            fields.append(field)
    return fields


def operation_request_dto(op_def: Node, sdl: bytes) -> str | None:
    """DTO from the operation's variable definitions — the ``$input``/``$data`` variable if
    present, else the sole variable — as its type, as written. ``None`` if 0 or many unnamed."""
    var_defs = child_of_type(op_def, "variable_definitions")
    if var_defs is None:
        return None
    variables = [c for c in var_defs.named_children if c.type == "variable_definition"]
    if not variables:
        return None

    def var_name(v: Node) -> str | None:
        var = child_of_type(v, "variable")
        n = child_of_type(var, "name") if var is not None else None
        return node_text(n, sdl) if n is not None else None

    chosen = next((v for v in variables if var_name(v) in ("input", "data")), None)
    if chosen is None:
        if len(variables) != 1:
            return None
        chosen = variables[0]
    return type_text(child_of_type(chosen, "type"), sdl)
