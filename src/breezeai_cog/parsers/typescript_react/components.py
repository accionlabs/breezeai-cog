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

A **hook** is marked ``uiRole="hook"`` by the exact mirror of the Vue composable rule: a function
that is BOTH named ``useX`` AND calls a React hook primitive (``useState`` / ``useEffect`` / … )
imported from ``react``. The name says "intended as a hook"; the primitive call makes it
verifiable (a ``useX``-named plain util that touches no React state is NOT marked). Hooks are
camelCase (``useX``) and components are PascalCase, so the two roles never collide on the name
axis. Known honest gap: a hook that only wraps *another custom* hook and never calls a built-in
directly stays ``uiRole=None`` — a recoverable gap (its ``calls[]`` edge to the wrapped hook is
captured, so a transitive ``CALLS``-closure query or a later pass can recover it), never a guess."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...schemas import FileRecord
from ..treesitter import line_span
from ..typescript.imports import imported_locals

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

# React hook primitives — calling one (imported from ``react``) is the structural mark of a hook.
# A custom hook is named ``useX`` by convention; combined with a primitive call that convention
# becomes verifiable (a ``useX``-named util with no primitive call is NOT marked). Includes the
# React 18 concurrent hooks; ``use`` (React 19) is excluded — ``^use[A-Z]`` requires a following
# uppercase, and a bare ``use`` call alone is too weak a signal.
_REACT_HOOKS = frozenset(
    {
        "useState",
        "useEffect",
        "useRef",
        "useContext",
        "useReducer",
        "useMemo",
        "useCallback",
        "useLayoutEffect",
        "useImperativeHandle",
        "useDebugValue",
        "useTransition",
        "useDeferredValue",
        "useId",
        "useSyncExternalStore",
        "useInsertionEffect",
    }
)
# A hook is named `useX` by convention; the primitive call (above) verifies it.
_HOOK_NAME = re.compile(r"^use[A-Z]")


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


def mark_react_hooks(root: Node, source: bytes, record: FileRecord) -> None:
    """Mark ``uiRole="hook"`` on React custom hooks — a function that is BOTH named ``useX``
    AND calls a React hook primitive imported from ``react`` (see module docstring). Mirrors the
    Vue composable rule; the ``useX`` vs PascalCase name split keeps hooks and components
    disjoint, so this never conflicts with ``mark_react_components``."""
    if b"react" not in source:
        return
    hooks = imported_locals(root, source, "react", _REACT_HOOKS)
    if not hooks:
        return
    for fn in record.functions:
        if (
            fn.uiRole is None
            and fn.name is not None
            and _HOOK_NAME.match(fn.name)
            and any(call.name in hooks for call in fn.calls)
        ):
            fn.uiRole = "hook"
