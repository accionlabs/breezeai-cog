"""What a name means at one point of a TypeScript / JavaScript file: a local, a top-level
declaration of the file, or an import — and, for a constant, which file's value it reads.

A route path, prefix or address built from a name (``BASE + '/users'``) folds to the name's value
only when the file itself shows where the value comes from. The repository's constants are keyed
by the file that declares them (``src/config.ts::BASE``, see ``imports.build_ts_index``), and
:class:`ConstView` is the one way to read them from a file:

==========================================  ==================================================
The name, as this file sees it              Value
==========================================  ==================================================
a parameter or local of an enclosing         none — read at runtime, even when a constant of
function or block                            that name exists (:func:`local_scopes`)
imported (``import { BASE } from './c'``,    the constant ``BASE`` that ``./c`` itself declares
``const { BASE } = require('./c')``,         (``import * as c`` → ``c.BASE`` the same); a
``import * as c``)                           re-export or a default import → none
declared at the top of this file             this file's own constant (a ``const`` / ``readonly``
                                             / enum literal); any other declaration → none
anything else (a browser global, a typo)     none
==========================================  ==================================================

"None" means the value is left unread: callers mark it ``${name}`` (``addresses.py``) or leave
the address empty — never another file's constant of the same name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Node

from ..treesitter import node_text

#: Nodes that open a scope a ``const`` / ``let`` / parameter can shadow a top-level name in: a
#: function, a block, a loop (``for (let x …)``), a ``switch`` body and a ``catch`` clause.
_SCOPES = frozenset({"function_declaration", "generator_function_declaration",
                     "function_expression", "function", "generator_function", "arrow_function",
                     "method_definition", "statement_block", "for_statement", "for_in_statement",
                     "switch_body", "catch_clause"})
#: The scopes a ``var`` belongs to: it is hoisted to its function, past any block or loop.
_FUNCTIONS = frozenset({"function_declaration", "generator_function_declaration",
                        "function_expression", "function", "generator_function", "arrow_function",
                        "method_definition"})
#: Declarations that bind their name in the enclosing scope.
_NAMED_DECLS = frozenset({"function_declaration", "generator_function_declaration",
                          "class_declaration", "abstract_class_declaration"})
#: A scope's own bindings: a function's parameters, ``catch (e)``, ``for (const x of …)``.
_SCOPE_BINDINGS = ("parameters", "parameter", "left")
#: Top-level declarations whose name the file declares (``export`` unwrapped).
_TOP_NAMED = frozenset({*_NAMED_DECLS, "enum_declaration", "class", "module",
                        "internal_module"})


def pattern_names(node: Node | None, source: bytes) -> set[str]:
    """The names a binding pattern binds: ``x``, ``{ a, b: c }``, ``[d, ...e]``, ``f = 1``."""
    if node is None:
        return set()
    if node.type in ("identifier", "shorthand_property_identifier_pattern"):
        return {node_text(node, source)}
    if node.type in ("assignment_pattern", "object_assignment_pattern"):
        return pattern_names(node.child_by_field_name("left") or node.named_children[0], source)
    if node.type == "pair_pattern":
        return pattern_names(node.child_by_field_name("value"), source)
    if node.type in ("required_parameter", "optional_parameter"):
        return pattern_names(node.child_by_field_name("pattern"), source)
    out: set[str] = set()
    if node.type in ("object_pattern", "array_pattern", "rest_pattern", "formal_parameters"):
        for c in node.named_children:
            out |= pattern_names(c, source)
    return out


@dataclass(frozen=True, slots=True)
class Scope:
    """One function / block / loop / ``catch`` below the top level and the names declared
    directly in it."""

    start_byte: int
    end_byte: int
    start_line: int
    end_line: int
    names: frozenset[str]


def _is_var(loop: Node, source: bytes) -> bool:
    """Whether a ``for (… of / in …)`` declares its variable with ``var``."""
    kind = loop.child_by_field_name("kind")
    return kind is not None and node_text(kind, source) == "var"


def local_scopes(root: Node, source: bytes) -> list[Scope]:
    """Every function / block / loop / ``catch`` below the top level that declares a name. A
    ``var`` is its enclosing function's, wherever in the body it is written. Top-level names are
    not in it: they are what a local shadows. Walked once, with an explicit stack (a deep
    expression must not exhaust recursion)."""
    scopes: list[tuple[Node, set[str]]] = []
    stack: list[tuple[Node, set[str] | None, set[str] | None]] = [(root, None, None)]
    while stack:
        node, names, fn = stack.pop()
        own = names
        if node.type in _SCOPES:
            own = set()
            scopes.append((node, own))
            if node.type in _FUNCTIONS:
                fn = own
            for fld in _SCOPE_BINDINGS:
                bound = node.child_by_field_name(fld)
                if bound is not None and (fld != "left" or node.type == "for_in_statement"):
                    hoisted = fld == "left" and _is_var(node, source)
                    (fn if fn is not None and hoisted else own).update(
                        pattern_names(bound, source)
                    )
        if names is not None:  # the top level (``names is None``) is not a local scope
            if node.type == "variable_declarator":
                parent = node.parent
                var = parent is not None and parent.type == "variable_declaration"
                (fn if fn is not None and var else names).update(
                    pattern_names(node.child_by_field_name("name"), source)
                )
            elif node.type in _NAMED_DECLS:
                nm = node.child_by_field_name("name")
                if nm is not None:
                    names.add(node_text(nm, source))
        stack.extend((c, own, fn) for c in node.named_children)
    return [Scope(n.start_byte, n.end_byte, n.start_point[0] + 1, n.end_point[0] + 1,
                  frozenset(names))
            for n, names in scopes if names]


def top_level_names(root: Node, source: bytes) -> frozenset[str]:
    """Every name the file declares at its top level (``export`` unwrapped): variables (any
    pattern), functions, classes, enums, namespaces. Imports are not declarations here."""
    out: set[str] = set()
    for top in root.named_children:
        node = top
        if top.type == "export_statement":
            node = top.child_by_field_name("declaration") or next(
                (c for c in top.named_children if c.type in _TOP_NAMED
                 or c.type in ("lexical_declaration", "variable_declaration")), top)
        if node.type in ("lexical_declaration", "variable_declaration"):
            for d in node.named_children:
                if d.type == "variable_declarator":
                    out |= pattern_names(d.child_by_field_name("name"), source)
        elif node.type in _TOP_NAMED:
            nm = node.child_by_field_name("name") or next(
                (c for c in node.named_children if c.type in ("identifier", "type_identifier")),
                None)
            if nm is not None:
                out.add(node_text(nm, source))
    return frozenset(out)


#: Separates a constant's file from its symbol in the repository's constants table.
KEY_SEP = "::"


def const_key(file: str, symbol: str) -> str:
    """The constants-table key of ``symbol`` (``BASE``, ``Routes.users``) declared in ``file``."""
    return f"{file}{KEY_SEP}{symbol}"


@dataclass
class ConstView:
    """The repository's constants as one file sees them (see the module docstring). ``get``
    keeps the ``dict`` reading every caller uses; ``value`` adds the point of use, so a local of
    the same name hides the constant."""

    rel: str
    table: dict[str, str | None]
    own: frozenset[str]
    #: Local name → (the file its module resolves to, or None; the export it binds).
    imports: dict[str, tuple[str | None, str]]
    root: Node | None = None
    source: bytes = b""
    _locals: dict[str, list[tuple[int, int]]] | None = field(default=None, repr=False)

    def is_local(self, name: str, at: Node) -> bool:
        """Whether ``name``, read at ``at``, is a parameter or local of a scope around it."""
        if self.root is None:
            return False
        if self._locals is None:
            ranges: dict[str, list[tuple[int, int]]] = {}
            for s in local_scopes(self.root, self.source):
                for n in s.names:
                    ranges.setdefault(n, []).append((s.start_byte, s.end_byte))
            self._locals = ranges
        pos = at.start_byte
        return any(a <= pos < b for a, b in self._locals.get(name, ()))

    def key(self, symbol: str, at: Node | None = None) -> str | None:
        """The table key ``symbol`` reads here, or None when it reads no constant."""
        root, _, rest = symbol.partition(".")
        if not root or (at is not None and self.is_local(root, at)):
            return None
        bound = self.imports.get(root)
        if bound is not None:
            file, export = bound
            if file is None or export == "default":
                return None
            if export == "*":
                return const_key(file, rest) if rest else None
            return const_key(file, f"{export}.{rest}" if rest else export)
        if root in self.own:
            return const_key(self.rel, symbol)
        return None

    def value(self, symbol: str, at: Node | None = None) -> str | None:
        k = self.key(symbol, at)
        return self.table.get(k) if k is not None else None

    def get(self, symbol: str | None, default: Any = None) -> Any:
        if symbol is None:
            return default
        v = self.value(symbol)
        return default if v is None else v


def const_view(
    root: Node, source: bytes, rel: str, table: dict[str, str | None] | None,
    resolve_module: Callable[[str], str | None],
) -> ConstView:
    """The constants ``rel`` sees: its imports resolved with ``resolve_module``, its top-level
    names, and (lazily) its local scopes."""
    from .declarations import import_bindings
    from .imports import relative_requires

    bindings = {**relative_requires(root, source), **import_bindings(root, source)}
    imports = {local: (resolve_module(module), export)
               for local, (module, export) in bindings.items()}
    return ConstView(rel, table or {}, top_level_names(root, source), imports, root, source)


def file_consts(index: object | None, root: Node, source: bytes, path: str) -> ConstView:
    """The view a detector folds with: the index's constants, imports resolved as the TS parser
    resolves them (relative, then tsconfig / Vite aliases)."""
    from .imports import TsAliasIndex, _resolve

    idx = index if isinstance(index, TsAliasIndex) else None
    table = idx.const_values if idx is not None else {}
    repo_root = Path(idx.base_dir) if idx is not None else None

    def resolve(module: str) -> str | None:
        return _resolve(module, path, repo_root, idx) if repo_root is not None else None

    return const_view(root, source, path, table, resolve)


def collect_consts(root: Node, source: bytes, rel: str, repo_root: Path | None,
                   table: dict[str, str | None] | None = None) -> ConstView:
    """The view a registered fact's ``collect`` qualifies names with, before every file's
    constants are known: relative imports only (the alias index is not built in the workers).
    ``table`` may hold this file's own constants, keyed with :func:`const_key`."""
    from .declarations import _resolve_relative

    return const_view(root, source, rel, table,
                      lambda module: _resolve_relative(module, rel, repo_root))
