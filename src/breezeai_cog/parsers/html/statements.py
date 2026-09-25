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

import re

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

#: Angular's page-navigation directive. Matched EXACTLY so ``routerLinkActive`` (a CSS-class
#: directive, not navigation) is not treated as a link. Written ``routerLink`` (plain string
#: target) or ``[routerLink]`` (an expression / commands array).
_NAV_DIRECTIVE = "routerLink"
#: The first string literal in a ``routerLink`` value — the static base of the target route.
_LITERAL_RE = re.compile(r"""['"]([^'"]+)['"]""")


def _child(node: Node, kind: str) -> Node | None:
    return next((c for c in node.named_children if c.type == kind), None)


def _is_navigation(node: Node, source: bytes) -> bool:
    """Whether ``node`` is a ``routerLink`` navigation — a plain ``attribute`` named
    ``routerLink`` (``routerLink="/x"``) or a ``property_binding`` whose target is
    ``routerLink`` (``[routerLink]="…"``). Exact-name so ``routerLinkActive`` is excluded."""
    if node.type == "attribute":
        an = _child(node, "attribute_name")
        return an is not None and node_text(an, source) == _NAV_DIRECTIVE
    if node.type == "property_binding":
        return _binding_name(node, source) == _NAV_DIRECTIVE
    return False


def _nav_endpoint(node: Node, source: bytes) -> str | None:
    """The target route of a ``routerLink`` — honest-null unless it resolves to a literal.
    ``routerLink="/orders"`` → ``/orders``; ``[routerLink]="'/home'"`` → ``/home``;
    ``[routerLink]="['/orders', id]"`` → ``/orders`` (the static base); a bare dynamic
    expression (``[routerLink]="target"``) → ``None`` (never the symbol name)."""
    if node.type == "attribute":
        av = _child(node, "quoted_attribute_value")
        val = _child(av, "attribute_value") if av is not None else None
        return node_text(val, source) if val is not None else None
    expr = _child(node, "expression")
    if expr is None:
        return None
    text = node_text(expr, source).strip()
    # A pure string literal, or an array/commands literal whose first element is a string.
    if text.startswith(("'", '"', "[")):
        m = _LITERAL_RE.search(text)
        return m.group(1) if m else None
    return None


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
    root: Node,
    source: bytes,
    path: str,
    parent_id: str,
    seen_ids: set[str],
    limit: int,
    *,
    framework: str,
    emit_routes: bool = True,
) -> list[Statement]:
    """Walk the template tree and emit one flat Statement per captured construct.

    ``routerLink`` navigation becomes a ``route`` statement (``routeKind="navigation"``) so the
    page→page navigation graph is queryable — gated by ``emit_routes`` (off for fixture files,
    like every other route emitter). ``framework`` stamps that route (the template's framework).
    """
    out: list[Statement] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        start, col = node.start_point[0] + 1, node.start_point[1]
        text = node_text(node, source)
        clip = text if len(text) <= limit else text[:limit]

        if emit_routes and _is_navigation(node, source):
            out.append(
                Statement(
                    id=disambiguate(statement_id(path, start, col), seen_ids),
                    parentId=parent_id,
                    nodeType=node.type,  # real grammar node (attribute / property_binding)
                    semanticType="route",
                    framework=framework,
                    routeKind="navigation",
                    endpoint=_nav_endpoint(node, source),  # honest-null when dynamic
                    name=_NAV_DIRECTIVE,
                    text=clip,
                    startLine=start,
                    endLine=node.end_point[0] + 1,
                    path=path,
                )
            )
            continue  # a nav link is one route statement; don't also emit it as a binding
        if node.type in _CAPTURED:
            out.append(
                Statement(
                    id=disambiguate(statement_id(path, start, col), seen_ids),
                    parentId=parent_id,
                    nodeType=node.type,  # real angular-grammar node type
                    text=clip,
                    name=_name_for(node, source),
                    handler=_handler(node, source) if node.type == "event_binding" else None,
                    startLine=start,
                    endLine=node.end_point[0] + 1,
                    path=path,
                )
            )
        stack.extend(reversed(node.named_children))
    return out
