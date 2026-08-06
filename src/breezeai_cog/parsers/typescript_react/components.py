"""React ``uiRole`` marking — class and function components, from structural signals only.

Two honest signals mark a node the base parser already captured (no new node, no re-parenting):

  * **Class component** — a class whose ``extends`` is a React base (``React.Component`` /
    ``Component`` / ``PureComponent``). Read straight off ``Class.extends``; exact.
  * **Function component** — a function that is BOTH named PascalCase AND renders JSX. The
    name says "intended as a component"; the JSX makes it verifiable (a PascalCase util that
    returns no JSX is NOT marked, matching the honesty rule). JSX presence is detected on the
    parse tree (a ``jsx_element`` / ``jsx_self_closing_element`` / ``jsx_fragment`` anywhere in
    the function's subtree — so components that render via ``.map`` / ternary / fragment count),
    then matched to the captured ``Function`` by start line. Using the record's own ``name``
    reuses the base parser's name derivation (``const X = () =>``, ``function X``, and
    ``export default React.memo(function X)`` are all covered without re-deriving names here).

``useX`` hooks are a naming heuristic with no verifiable structural signal, so — mirroring the
decision already taken for the Vue ``useX`` composable — the ``hook`` role is deferred; hooks
are camelCase and so are never caught by the PascalCase function-component gate."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import FileRecord
from ..treesitter import line_span

# React base classes a component may extend (bare or ``React.``-qualified). Compared against
# ``Class.extends`` with any generic arguments (``<Props, State>``) stripped.
_REACT_BASES = frozenset(
    {"Component", "PureComponent", "React.Component", "React.PureComponent"}
)

# Function-valued nodes whose ``line_span`` start matches a captured ``Function.startLine``.
_FN_TYPES = frozenset({"function_declaration", "arrow_function", "function_expression"})
_JSX_TYPES = frozenset({"jsx_element", "jsx_self_closing_element", "jsx_fragment"})

# A React component is PascalCase by convention (first char upper); combined with a JSX render
# that convention becomes verifiable. Hooks (``useX``) are camelCase and excluded by this gate.
_PASCAL_CASE = re.compile(r"^[A-Z]")


def _collect_jsx_fn_lines(node: Node, out: set[int]) -> bool:
    """Add the start line of every function-valued node whose subtree contains JSX to ``out``.
    Returns whether ``node``'s own subtree contains JSX (so ancestors accumulate it too)."""
    has_jsx = node.type in _JSX_TYPES
    for child in node.children:
        if _collect_jsx_fn_lines(child, out):
            has_jsx = True
    if has_jsx and node.type in _FN_TYPES:
        out.add(line_span(node)[0])
    return has_jsx


def mark_react_components(root: Node, source: bytes, record: FileRecord) -> None:
    """Set ``uiRole="component"`` on React class and function components (see module docstring)."""
    # Class components — read off the captured `extends` (generics stripped).
    for cls in record.classes:
        if cls.uiRole is None and cls.extends is not None:
            base = cls.extends.split("<", 1)[0].strip()
            if base in _REACT_BASES:
                cls.uiRole = "component"

    # Function components — PascalCase name AND a JSX-rendering body.
    jsx_fn_lines: set[int] = set()
    _collect_jsx_fn_lines(root, jsx_fn_lines)
    if not jsx_fn_lines:
        return
    for fn in record.functions:
        if (
            fn.uiRole is None
            and fn.name
            and _PASCAL_CASE.match(fn.name)
            and fn.startLine in jsx_fn_lines
        ):
            fn.uiRole = "component"
