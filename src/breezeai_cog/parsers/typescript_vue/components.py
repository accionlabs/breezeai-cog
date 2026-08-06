"""Factory-defined UI-role marking (Vue ``defineComponent`` / Pinia ``defineStore``).

Some UI entities are authored as ``const X = factory(...)`` from a specific library rather than
as a class or ``.vue`` SFC — Vue's ``defineComponent(...)`` (a component) and Pinia's
``defineStore(...)`` (a store). The base parser lifts their inner functions but never node-ifies
the binding, so the entity has no role marker. This sets ``uiRole`` on the node the parser
ALREADY captures — no new node, no member re-parenting:

  * ``export default factory(...)``           -> the File (``record.uiRole``); one per module.
  * ``export const X = factory(...)`` / ``const X = factory(...)`` (object or functional form)
                                              -> the binding's ``lexical_declaration`` statement.

Detection is keyed on the real import (name + source module, alias-resolved via the import
specifier), NOT the bare callee text — so a local function named ``defineStore`` is never
matched, and ``import { defineStore as ds }`` still works. Only top-level declarations / exports
are scanned; the walk never descends into a body. The statement-anchored form appears only under
``--capture-statements``; the default-export File form is always set."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import FileRecord
from ..treesitter import node_text
from ..typescript.imports import _module_of

# (imported name, source module, uiRole) — factory calls whose binding defines a UI entity.
_FACTORY_SPECS = (
    ("defineComponent", "vue", "component"),
    ("defineStore", "pinia", "store"),
)
_FACTORY_NAME_BYTES = tuple(name.encode() for name, _, _ in _FACTORY_SPECS)

# Vue reactivity-STATE primitives — calling one is the structural mark of composition-API
# reactive logic. (Lifecycle hooks like onMounted are deliberately excluded: they appear in a
# component's own setup too, so they don't distinguish a composable from a component.)
_VUE_REACTIVITY = frozenset(
    {
        "ref",
        "reactive",
        "computed",
        "watch",
        "watchEffect",
        "shallowRef",
        "shallowReactive",
        "readonly",
        "toRef",
        "toRefs",
        "customRef",
        "triggerRef",
    }
)
# A composable is named `useX` by convention; combined with a reactivity call that convention
# becomes verifiable (a `useX`-named util with no reactive state is NOT marked).
_COMPOSABLE_NAME = re.compile(r"^use[A-Z]")


def _factory_locals(root: Node, source: bytes) -> dict[str, str]:
    """Each local name bound to a known factory import → its ``uiRole`` (handles ``as`` alias).
    ``import { defineStore as ds } from 'pinia'`` → ``{"ds": "store"}``."""
    by_module: dict[str, dict[str, str]] = {}
    for name, module, ui_role in _FACTORY_SPECS:
        by_module.setdefault(module, {})[name] = ui_role
    locals_map: dict[str, str] = {}
    for node in root.named_children:
        if node.type != "import_statement":
            continue
        roles = by_module.get(_module_of(node, source) or "")
        if roles is None:
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
                # `{ defineStore }` -> [defineStore]; `{ defineStore as ds }` -> [defineStore, ds].
                # First is the imported name, last is the local binding.
                if not idents:
                    continue
                role = roles.get(node_text(idents[0], source))
                if role is not None:
                    locals_map[node_text(idents[-1], source)] = role
    return locals_map


def _call_role(node: Node | None, locals_map: dict[str, str], source: bytes) -> str | None:
    """The ``uiRole`` if ``node`` is a ``factory(...)`` call to a resolved local name, else None."""
    if node is None or node.type != "call_expression":
        return None
    fn = node.child_by_field_name("function")
    if fn is None or fn.type != "identifier":
        return None
    return locals_map.get(node_text(fn, source))


def mark_factory_ui_roles(root: Node, source: bytes, record: FileRecord) -> None:
    """Set ``uiRole`` on factory-defined components/stores (see module docstring)."""
    if not any(name in source for name in _FACTORY_NAME_BYTES):  # cheap byte pre-guard
        return
    locals_map = _factory_locals(root, source)
    if not locals_map:
        return
    line_roles: dict[int, str] = {}  # `lexical_declaration` start line → uiRole
    for child in root.named_children:
        decl: Node | None = child
        if child.type == "export_statement":
            if any(c.type == "default" for c in child.children):
                call = next((c for c in child.named_children if c.type == "call_expression"), None)
                role = _call_role(call, locals_map, source)
                if role is not None:
                    record.uiRole = role  # export default factory(...) -> File
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
                if d.type == "variable_declarator":
                    role = _call_role(d.child_by_field_name("value"), locals_map, source)
                    if role is not None:
                        line_roles[decl.start_point[0] + 1] = role
    if line_roles:
        for s in record.statements:
            if s.nodeType == "lexical_declaration" and s.uiRole is None:
                role = line_roles.get(s.startLine)
                if role is not None:
                    s.uiRole = role


def _imported_locals(root: Node, source: bytes, module: str, wanted: frozenset[str]) -> set[str]:
    """Local names bound to any of ``wanted`` imported from ``module`` (handles ``as`` alias)."""
    locals_set: set[str] = set()
    for node in root.named_children:
        if node.type != "import_statement" or _module_of(node, source) != module:
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
                if idents and node_text(idents[0], source) in wanted:
                    locals_set.add(node_text(idents[-1], source))
    return locals_set


def mark_composables(root: Node, source: bytes, record: FileRecord) -> None:
    """Mark ``uiRole="composable"`` on Vue composables — a function that is BOTH named ``useX``
    AND calls a Vue reactivity primitive imported from ``vue``. The name says "intended as a
    composable"; the reactivity call verifies it genuinely produces reactive state (so a
    ``useX``-named plain util is not marked, and a component's own ``setup`` — which uses
    reactivity but isn't named ``useX`` — is not either)."""
    if b"vue" not in source:
        return
    reactivity = _imported_locals(root, source, "vue", _VUE_REACTIVITY)
    if not reactivity:
        return
    for fn in record.functions:
        if (
            fn.uiRole is None
            and fn.name is not None
            and _COMPOSABLE_NAME.match(fn.name)
            and any(call.name in reactivity for call in fn.calls)
        ):
            fn.uiRole = "composable"
