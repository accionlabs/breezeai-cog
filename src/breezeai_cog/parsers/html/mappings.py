"""Capability metadata for the component-template parser (``.html`` / ``.htm``).

``STATEMENT_TYPES`` are the real tree-sitter node types this parser emits as
``Statement.nodeType`` when it captures a component template. They come from the dedicated
``angular`` grammar (in ``tree_sitter_language_pack``), which models bindings and
interpolation as first-class nodes — so each record keeps its genuine grammar node type
(no ``synthetic``). Discovered empirically by dumping the grammar over real templates.
"""

from __future__ import annotations

#: ``angular`` grammar node types emitted as Statement.nodeType for a component template.
#: Bindings + interpolation + Angular 17 control-flow blocks. Child-component ``element``
#: usage (a composition edge) is deferred to P2 (needs a repo-wide selector index).
STATEMENT_TYPES: list[str] = [
    "interpolation",
    "event_binding",
    "property_binding",
    "two_way_binding",
    "structural_directive",
    "if_statement",
    "else_statement",
    "for_statement",
    "switch_statement",
    "defer_statement",
]

#: Frameworks whose templates this parser resolves and stamps (inherited from the owning
#: component). P1 covers Angular; ``.vue`` templates are handled inside the Vue parser.
FRAMEWORKS: list[str] = ["angular"]

#: Comment node type for the shared whole-file comment pass (HTML ``<!-- … -->``).
COMMENT_TYPES: frozenset[str] = frozenset({"comment"})
