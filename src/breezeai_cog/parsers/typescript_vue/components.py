"""Vue ``defineComponent(...)`` component-identity marking.

A component authored via ``defineComponent(...)`` (rather than a ``.vue`` SFC) is marked with
``uiRole="component"`` on the node the base parser ALREADY captures — no new node, no member
re-parenting:

  * ``export default defineComponent(...)``  -> the File (``record.uiRole``); one per module.
  * ``export const X = defineComponent(...)`` / ``const X = defineComponent(...)``
    (object or functional form)              -> the ``lexical_declaration`` statement for ``X``.

Detection is keyed on the real ``defineComponent`` import from ``vue`` (alias-resolved via the
import specifier), NOT the bare callee text — so an unrelated local ``defineComponent`` is never
matched, and ``import { defineComponent as dc }`` still works. Only top-level declarations /
exports are scanned; the walk never descends into a body (so a local named after the factory is
not mistaken for a component). The statement-anchored form only appears under
``--capture-statements`` (the default-export File form is always set)."""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..treesitter import node_text
from ..typescript.imports import _module_of

_UI_ROLE_COMPONENT = "component"


def _definecomponent_locals(root: Node, source: bytes) -> set[str]:
    """Local names bound to ``defineComponent`` from ``vue`` (handles ``as`` aliasing)."""
    names: set[str] = set()
    for node in root.named_children:
        if node.type != "import_statement" or _module_of(node, source) != "vue":
            continue
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            continue
        for c in clause.named_children:
            if c.type != "named_imports":
                continue
            for spec in c.named_children:
                if spec.type != "import_specifier":
                    continue
                idents = [x for x in spec.named_children if x.type == "identifier"]
                # `{ defineComponent }` -> [defineComponent]; `{ defineComponent as dc }` ->
                # [defineComponent, dc]. First is the imported name, last is the local binding.
                if idents and node_text(idents[0], source) == "defineComponent":
                    names.add(node_text(idents[-1], source))
    return names


def _is_dc_call(node: Node | None, dc_names: set[str], source: bytes) -> bool:
    """True if ``node`` is a ``defineComponent(...)`` call (callee resolves to a local name)."""
    if node is None or node.type != "call_expression":
        return False
    fn = node.child_by_field_name("function")
    return fn is not None and fn.type == "identifier" and node_text(fn, source) in dc_names


def mark_define_components(root: Node, source: bytes, record: FileRecord) -> None:
    """Set ``uiRole="component"`` on ``defineComponent``-defined components (see module doc)."""
    if b"defineComponent" not in source:
        return
    dc = _definecomponent_locals(root, source)
    if not dc:
        return
    decl_lines: set[int] = set()  # `lexical_declaration` lines that bind a defineComponent
    for child in root.named_children:
        decl: Node | None = child
        if child.type == "export_statement":
            if any(c.type == "default" for c in child.children):
                call = next((c for c in child.named_children if c.type == "call_expression"), None)
                if _is_dc_call(call, dc, source):
                    record.uiRole = (
                        _UI_ROLE_COMPONENT  # export default defineComponent(...) -> File
                    )
                    continue
            decl = next(
                (
                    c
                    for c in child.named_children
                    if c.type in ("lexical_declaration", "variable_declaration")
                ),
                None,
            )
        if decl is not None and decl.type in ("lexical_declaration", "variable_declaration"):
            for d in decl.named_children:
                if d.type == "variable_declarator" and _is_dc_call(
                    d.child_by_field_name("value"), dc, source
                ):
                    decl_lines.add(decl.start_point[0] + 1)
    if decl_lines:
        for s in record.statements:
            if (
                s.nodeType == "lexical_declaration"
                and s.startLine in decl_lines
                and s.uiRole is None
            ):
                s.uiRole = _UI_ROLE_COMPONENT
