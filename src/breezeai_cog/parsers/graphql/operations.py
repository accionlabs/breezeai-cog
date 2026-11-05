"""Reading a GraphQL operation document (``query GetOrders { orders { id } }``) — what a client
sends. Shared by every language's GraphQL client detector: the invoked API is each root
selection field, which joins to the server route's ``endpoint``."""

from __future__ import annotations

from tree_sitter import Node

from ..treesitter import node_text
from .sdl import child_of_type, type_text

#: ``operation_type`` keyword → ``routeKind``.
OPERATION_KINDS = {"query": "query", "mutation": "mutation", "subscription": "subscription"}


def fragment_definitions(root: Node) -> dict[str, Node]:
    """``fragment Name on T { … }`` definitions of one document, by name."""
    out: dict[str, Node] = {}
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "fragment_definition":
            fname = child_of_type(n, "fragment_name")
            name = child_of_type(fname, "name") if fname is not None else None
            if name is not None:
                out[(name.text or b"").decode()] = n
        elif n.type not in ("operation_definition", "selection_set"):
            stack.extend(n.named_children)
    return out


def root_fields(op_def: Node, fragments: dict[str, Node] | None = None) -> list[tuple[Node, Node]]:
    """``(anchor, field)`` for each root field an operation invokes — the actual API operations.
    The anchor is the operation's own root selection: the field itself, or the fragment spread
    (``...people``) / inline fragment that brings it in. A spread is followed into a fragment of
    the same document (``fragments``, see :func:`fragment_definitions`) — at the root it is on
    the root type, so its fields are root fields; a fragment defined elsewhere adds nothing."""
    sel_set = child_of_type(op_def, "selection_set")
    out: list[tuple[Node, Node]] = []
    if sel_set is None:
        return out

    def expand(selections: Node, anchor: Node | None, seen: frozenset[str]) -> None:
        for selection in selections.named_children:
            if selection.type != "selection" or not selection.named_children:
                continue
            item = selection.named_children[0]
            here = anchor if anchor is not None else item
            if item.type == "field":
                if child_of_type(item, "name") is not None:
                    out.append((here, item))
            elif item.type == "inline_fragment":
                inner = child_of_type(item, "selection_set")
                if inner is not None:
                    expand(inner, here, seen)
            elif item.type == "fragment_spread" and fragments:
                fname = child_of_type(item, "fragment_name")
                name = child_of_type(fname, "name") if fname is not None else None
                key = (name.text or b"").decode() if name is not None else ""
                frag = fragments.get(key)
                inner = child_of_type(frag, "selection_set") if frag is not None else None
                if inner is not None and key not in seen:
                    expand(inner, here, seen | {key})

    expand(sel_set, None, frozenset())
    return out


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
