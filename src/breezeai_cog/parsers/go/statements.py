"""Flat statement capture for Go (gated by --capture-statements)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import Statement
from ..statements_common import classify_statement, render_concat, resolve_endpoint
from ..treesitter import node_text
from .mappings import CONTROL_FLOW, EMIT_TYPES, NESTED_SCOPES

_CALL_TYPE = "call_expression"


def _name_of(node: Node, source: bytes) -> str | None:
    if node.type in {"var_declaration", "const_declaration"}:
        # `var x = 1` / `const (A = 1; B = 2)`: the names live on the var_spec/const_spec
        # children (a grouped declaration is named after its first spec).
        specs = [c for c in node.named_children if c.type != "var_spec_list"]
        for group in node.named_children:
            if group.type == "var_spec_list":  # `var ( … )` wraps its specs; `const ( … )` does not
                specs.extend(group.named_children)
        for spec in specs:
            if spec.type in {"var_spec", "const_spec"}:
                name = spec.child_by_field_name("name")
                if name is not None:
                    return node_text(name, source)
    if node.type == "short_var_declaration":
        left = node.child_by_field_name("left")
        if left is not None:
            for child in left.named_children:
                if child.type == "identifier":
                    return node_text(child, source)
    return None


def _render_url(node: Node, source: bytes) -> str | None:
    if node.type == "interpreted_string_literal":
        text = node_text(node, source)
        return text.strip('"')
    if node.type == "raw_string_literal":
        text = node_text(node, source)
        return text.strip('`')
    if node.type == "binary_expression":
        return render_concat(node, source, _render_url)
    return None


def _call_details(call: Node, source: bytes) -> tuple[str, str, str | None] | None:
    if call.type != "call_expression":
        return None
    fn = call.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "selector_expression":
        operand = fn.child_by_field_name("operand")
        field = fn.child_by_field_name("field")
        callee = f"{node_text(operand, source)}.{node_text(field, source)}" if operand is not None and field is not None else node_text(fn, source)
        method = node_text(field, source) if field is not None else ""
    else:
        callee = node_text(fn, source)
        method = callee
    args = call.child_by_field_name("arguments")
    arg_nodes: list[Node] = list(args.named_children) if args is not None else []
    endpoint, override = resolve_endpoint(arg_nodes, source, _render_url)
    if override is not None:
        method = override
    return callee, method, endpoint


#: Declarations that bind names to values: ``var x = …`` (one ``var_spec`` each) and ``x := …``.
_BINDING_OWNERS = ("var_spec", "short_var_declaration")


def _pairs(owner: Node) -> list[tuple[Node, Node]] | None:
    """(name, value) per binding of a ``var_spec`` / ``short_var_declaration``, or None when
    the names and values do not pair up (``a, b := f()``) or there are no values."""
    if owner.type == "var_spec":
        names = owner.children_by_field_name("name")
        values = owner.child_by_field_name("value")
    else:
        left = owner.child_by_field_name("left")
        names = list(left.named_children) if left is not None else []
        values = owner.child_by_field_name("right")
    if values is None:
        return None
    vals = [v for v in values.named_children if v.type != "comment"]
    if not names or len(names) != len(vals):
        return None
    return list(zip(names, vals))


def bound_name(literal: Node, source: bytes) -> str | None:
    """The variable a ``func_literal`` is stored in (``var notify = func…``, ``cb := func…``),
    else None — an inline literal (an argument, a ``go``/``defer`` call, an assignment)."""
    values = literal.parent
    owner = values.parent if values is not None and values.type == "expression_list" else None
    if owner is None or owner.type not in _BINDING_OWNERS:
        return None
    for name, value in _pairs(owner) or []:
        if (value.start_byte, value.end_byte) == (literal.start_byte, literal.end_byte):
            text = node_text(name, source)
            return text if name.type == "identifier" and text != "_" else None
    return None


def bound_literals(statement: Node) -> list[Node] | None:
    """The function literals a ``var`` / ``:=`` statement stores, when that is all it stores
    (every value is a ``func_literal`` bound to a name), else None."""
    if statement.type == "short_var_declaration":
        owners = [statement]
    elif statement.type == "var_declaration":
        owners = [c for c in statement.named_children if c.type == "var_spec"]
        for lst in (c for c in statement.named_children if c.type == "var_spec_list"):
            owners.extend(c for c in lst.named_children if c.type == "var_spec")
    else:
        return None
    literals: list[Node] = []
    for owner in owners:
        pairs = _pairs(owner)
        if pairs is None:
            return None
        for name, value in pairs:
            if value.type != "func_literal" or name.type != "identifier" or name.text == b"_":
                return None
            literals.append(value)
    return literals or None


def _duplicates_functions(node: Node, barriers: frozenset[tuple[int, int]]) -> bool:
    """True for a ``var`` / ``:=`` statement that only stores function literals extracted as
    Functions (their spans in ``barriers``): the statement would repeat them."""
    literals = bound_literals(node) if barriers else None
    return literals is not None and all((n.start_byte, n.end_byte) in barriers for n in literals)


def _iter_in_scope(
    node: Node, descend_all: bool = False, barriers: frozenset[tuple[int, int]] = frozenset(),
) -> list[Node]:
    """Statement nodes in ``node``. The walk stops at declared functions and methods (unless
    ``descend_all``) and always at a function literal stored in a variable (``barriers``):
    each is its own Function. Inline literals are walked, so their statements belong to the
    enclosing function, or to the file at package level."""
    out: list[Node] = []
    for child in node.named_children:
        if not descend_all and child.type in NESTED_SCOPES:
            continue
        if (child.start_byte, child.end_byte) in barriers or _duplicates_functions(child, barriers):
            continue
        if child.type in EMIT_TYPES:
            out.append(child)
        out.extend(_iter_in_scope(child, descend_all, barriers))
    return out


def extract_statements(
    body: Node | None,
    source: bytes,
    path: str,
    *,
    parent_id: str,
    capture: bool,
    limit: int,
    seen_ids: set[str],
    descend_all: bool = False,
    barriers: frozenset[tuple[int, int]] = frozenset(),
) -> list[Statement]:
    if not capture or body is None:
        return []
    out: list[Statement] = []
    for node in _iter_in_scope(body, descend_all, barriers):
        out.extend(
            classify_statement(
                node,
                source,
                path,
                parent_id=parent_id,
                limit=limit,
                seen_ids=seen_ids,
                emit_types=EMIT_TYPES,
                control_flow=CONTROL_FLOW,
                call_type=_CALL_TYPE,
                name_of=_name_of,
                call_details=_call_details,
                language="go",
                decorators=[],
            )
        )
    return out
