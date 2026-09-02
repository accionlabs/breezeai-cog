"""Turn an ``angular``-grammar template tree into flat ``Statement`` records.

The dedicated ``angular`` grammar models every construct we care about as a real node, so
each statement keeps its genuine ``nodeType`` (``interpolation`` / ``event_binding`` /
``property_binding`` / ``two_way_binding`` / ``structural_directive`` / the ``@if``/``@for``
control-flow blocks) — never ``synthetic``, and never a new ``semanticType`` (ordinary markup
is structure + reference, not a route/db/event). The binding target lands in ``name`` and, for
an event binding that calls a method, the referenced member lands in ``handler`` (honest-null
when the expression is not a call), so a component method can be joined to the template
bindings that invoke it. Statements are flat; nesting (a binding inside ``@for``) is expressed
by line containment, like every other language.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text

#: Template nodes captured as statements (see module docstring). ``element`` (child-component
#: usage) is intentionally excluded here — it is P2 (needs the repo-wide selector index).
_BINDINGS = frozenset(
    {"event_binding", "property_binding", "two_way_binding", "structural_directive"}
)
_CONTROL_FLOW = frozenset(
    {"if_statement", "else_statement", "for_statement", "switch_statement", "defer_statement"}
)
_CAPTURED = _BINDINGS | _CONTROL_FLOW | {"interpolation"}


def _child(node: Node, kind: str) -> Node | None:
    return next((c for c in node.named_children if c.type == kind), None)


def _binding_name(node: Node, source: bytes) -> str | None:
    """The bound identifier — ``click`` for ``(click)``, ``id`` for ``[id]``, ``ngModel`` for
    ``[(ngModel)]``. Read from the grammar's ``binding_name`` node."""
    bn = _child(node, "binding_name")
    ident = _child(bn, "identifier") if bn is not None else None
    return node_text(ident, source) if ident is not None else None


def _directive_name(node: Node, source: bytes) -> str | None:
    """The directive name for a ``*ngFor``/``*ngIf`` — the first ``identifier`` child."""
    ident = _child(node, "identifier")
    return node_text(ident, source) if ident is not None else None


def _interpolation_name(node: Node, source: bytes) -> str | None:
    """The interpolation expression, e.g. ``user.name`` for ``{{ user.name }}``."""
    expr = _child(node, "expression")
    return node_text(expr, source).strip() if expr is not None else None


def _handler(node: Node, source: bytes) -> str | None:
    """For an event binding, the referenced component member — the callee of the first
    ``call_expression`` in the bound expression: ``load`` for ``(click)="load()"``, ``remove``
    for ``remove(o)``, ``svc.save`` → ``save``. Honest-null when the expression is an
    assignment or bare value (no call)."""
    call = _find(node, "call_expression")
    if call is None:
        return None
    callee = call.named_children[0] if call.named_children else None
    if callee is None:
        return None
    if callee.type == "member_expression":
        idents = [c for c in callee.named_children if c.type == "identifier"]
        return node_text(idents[-1], source) if idents else None
    if callee.type == "identifier":
        return node_text(callee, source)
    return None


def _find(node: Node, kind: str) -> Node | None:
    """First descendant of ``node`` (inclusive) whose type is ``kind`` (pre-order)."""
    if node.type == kind:
        return node
    for c in node.named_children:
        hit = _find(c, kind)
        if hit is not None:
            return hit
    return None


def _name_for(node: Node, source: bytes) -> str | None:
    if node.type == "interpolation":
        return _interpolation_name(node, source)
    if node.type == "structural_directive":
        return _directive_name(node, source)
    if node.type in ("event_binding", "property_binding", "two_way_binding"):
        return _binding_name(node, source)
    return None  # control-flow blocks: the nodeType is the whole story


def collect_template_statements(
    root: Node, source: bytes, path: str, parent_id: str, seen_ids: set[str], limit: int
) -> list[Statement]:
    """Walk the template tree and emit one flat Statement per captured construct."""
    out: list[Statement] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type in _CAPTURED:
            start, col = node.start_point[0] + 1, node.start_point[1]
            text = node_text(node, source)
            out.append(
                Statement(
                    id=disambiguate(statement_id(path, start, col), seen_ids),
                    parentId=parent_id,
                    nodeType=node.type,  # real angular-grammar node type
                    text=text if len(text) <= limit else text[:limit],
                    name=_name_for(node, source),
                    handler=_handler(node, source) if node.type == "event_binding" else None,
                    startLine=start,
                    endLine=node.end_point[0] + 1,
                    path=path,
                )
            )
        stack.extend(reversed(node.named_children))
    return out
