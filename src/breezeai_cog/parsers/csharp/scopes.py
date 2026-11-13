"""The declared type of a name at a point in a C# file, bound the way C# binds it.

One walk records every declaration under the scope that owns it:

- a **type** (class, struct, record, interface): its fields, properties and primary-constructor
  parameters;
- a **member** (method, constructor, accessor, operator, local function, indexer): its
  parameters and locals — lambdas and anonymous methods inside it are folded in, since their
  statements are attributed to the member; a lambda outside any member (a field initializer)
  is a member scope of its own;
- the **file**: the locals of top-level statements.

A name used at a node is then looked up from the innermost scope outward: the member's locals and
parameters first (a local of another type *shadows* the field — the call is not on the field),
then the type's fields and properties, its same-file base types, then each outer type. ``this.x``
looks at the type (and its bases) only. Two declarations of one name in the same scope (sibling
blocks) are both returned; a caller trusts them only when they agree.

One flat ``name → type`` map per file let the last class's field win for every class; this is
what keeps ``_sqs`` an ``IAmazonSQS`` in one class and an ``IMyQueue`` in the next.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import NamedTuple

from tree_sitter import Node

from ..treesitter import node_text

#: Declarations whose names are fields / properties (and C# 12 primary-constructor parameters).
TYPE_SCOPES = frozenset({
    "class_declaration", "struct_declaration", "record_declaration",
    "record_struct_declaration", "interface_declaration",
})
#: Declarations whose parameters and locals shadow a field.
MEMBER_SCOPES = frozenset({
    "method_declaration", "constructor_declaration", "destructor_declaration",
    "local_function_statement", "accessor_declaration", "operator_declaration",
    "conversion_operator_declaration", "indexer_declaration",
})
_LAMBDAS = frozenset({"lambda_expression", "anonymous_method_expression"})


class Binding(NamedTuple):
    """One declaration of a name: its type as written (``None`` for ``var`` or an untyped lambda
    parameter) and its initializer, if any."""

    type: str | None
    init: Node | None


#: A binding → a label (``aws-s3``, ``dynamodb``, ``http``…), or None when it is not one.
Classify = Callable[[Binding], "str | None"]


class _Scope:
    __slots__ = ("kind", "parent", "bindings", "key", "bases")

    def __init__(self, kind: str, parent: _Scope | None, key: str = "",
                 bases: tuple[str, ...] = ()) -> None:
        self.kind = kind  # "type" | "member" | "file"
        self.parent = parent
        self.bindings: dict[str, list[Binding]] = {}
        self.key = key  # a type's dotted name (``Outer.Inner``) — partial parts share it
        self.bases = bases  # a type's base type simple names

    def add(self, name: str | None, binding: Binding) -> None:
        if name:
            self.bindings.setdefault(name, []).append(binding)


def _span(node: Node) -> tuple[str, int, int]:
    return node.type, node.start_byte, node.end_byte


_BASE_NAME = re.compile(r"^\s*(?:global::)?([\w.]+)")


def _base_names(decl: Node, source: bytes) -> tuple[str, ...]:
    """Simple names of the types in a declaration's ``base_list`` (``Base<T>`` → ``Base``,
    ``Ns.Base(x)`` → ``Base``)."""
    bl = next((c for c in decl.named_children if c.type == "base_list"), None)
    out: list[str] = []
    for c in bl.named_children if bl is not None else []:
        m = _BASE_NAME.match(node_text(c, source))
        if m:
            out.append(m.group(1).rsplit(".", 1)[-1])
    return tuple(out)


def _text(node: Node | None, source: bytes) -> str | None:
    return node_text(node, source) if node is not None else None


def _type_text(node: Node | None, source: bytes) -> str | None:
    """A declared type as written; ``None`` for ``var`` (``implicit_type``) or no type."""
    if node is None or node.type == "implicit_type":
        return None
    return node_text(node, source)


class TypeScopes:
    """Every declaration of one C# file under the scope that owns it (built in one walk)."""

    def __init__(self, root: Node, source: bytes) -> None:
        self._source = source
        self._file = _Scope("file", None)
        self._scopes: dict[tuple[str, int, int], _Scope] = {}
        self._types: dict[str, list[_Scope]] = {}  # dotted name → its (partial) parts
        self._simple: dict[str, set[str]] = {}  # simple name → dotted names
        self._all: list[_Scope] = [self._file]
        self._walk(root, self._file, "")

    # -- build -------------------------------------------------------------------------------

    def _open(self, node: Node, scope: _Scope) -> None:
        self._scopes[_span(node)] = scope
        self._all.append(scope)

    def _walk(self, node: Node, scope: _Scope, owner: str) -> None:
        source = self._source
        for c in node.named_children:
            t = c.type
            if t in TYPE_SCOPES:
                name = _text(c.child_by_field_name("name"), source) or ""
                key = f"{owner}.{name}" if owner else name
                inner = _Scope("type", scope, key, _base_names(c, source))
                self._open(c, inner)
                self._types.setdefault(key, []).append(inner)
                self._simple.setdefault(name, set()).add(key)
                self._walk(c, inner, key)  # a primary-constructor ``parameter_list`` lands here
                continue
            if t == "delegate_declaration":  # its parameters declare nothing usable
                continue
            if t in MEMBER_SCOPES or (t in _LAMBDAS and scope.kind == "type"):
                inner = _Scope("member", scope)
                self._open(c, inner)
                self._walk(c, inner, owner)
                continue
            if t == "variable_declaration":  # a field, a local, a ``using`` / ``for`` variable
                typ = _type_text(c.child_by_field_name("type"), source)
                for d in c.named_children:
                    if d.type == "variable_declarator" and d.named_children:
                        nm = d.child_by_field_name("name") or d.named_children[0]
                        init = d.named_children[1] if len(d.named_children) > 1 else None
                        scope.add(node_text(nm, source), Binding(typ, init))
            elif t == "property_declaration":
                scope.add(_text(c.child_by_field_name("name"), source),
                          Binding(_type_text(c.child_by_field_name("type"), source),
                                  c.child_by_field_name("value")))
            elif t in ("parameter", "catch_declaration", "declaration_expression",
                       "declaration_pattern"):
                pn = c.child_by_field_name("name")
                if pn is not None and pn.type == "identifier":
                    scope.add(node_text(pn, source),
                              Binding(_type_text(c.child_by_field_name("type"), source), None))
            elif t == "implicit_parameter":  # ``x => …``
                scope.add(node_text(c, source), Binding(None, None))
            elif t == "foreach_statement":
                left = c.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    scope.add(node_text(left, source),
                              Binding(_type_text(c.child_by_field_name("type"), source), None))
            self._walk(c, scope, owner)

    # -- lookup ------------------------------------------------------------------------------

    def _scope_at(self, node: Node) -> _Scope:
        n: Node | None = node
        while n is not None:
            scope = self._scopes.get(_span(n))
            if scope is not None:
                return scope
            n = n.parent
        return self._file

    def _in_type(self, key: str, name: str, seen: set[str]) -> list[Binding] | None:
        """``name`` declared by the type ``key`` (all its partial parts in this file), else by
        its same-file base types — a base name that matches one type only."""
        seen.add(key)
        parts = self._types.get(key, [])
        found = [b for p in parts for b in p.bindings.get(name, [])]
        if found:
            return found
        for base in {b for p in parts for b in p.bases}:
            keys = self._simple.get(base, set())
            if len(keys) == 1 and (k := next(iter(keys))) not in seen:
                inherited = self._in_type(k, name, seen)
                if inherited is not None:
                    return inherited
        return None

    def lookup(self, node: Node, name: str, *, this_only: bool = False) -> list[Binding] | None:
        """The declarations ``name`` binds to at ``node``, innermost scope first; ``None`` when
        it is not declared in this file where it is used (inherited from another file, a static
        member, a name from a ``using``). ``this_only``: the ``x`` of ``this.x``."""
        scope: _Scope | None = self._scope_at(node)
        in_type = False
        while scope is not None:
            if scope.kind == "type":
                found = self._in_type(scope.key, name, set())
                if found is not None or this_only:
                    return found
                in_type = True
            elif not this_only and not (in_type and scope.kind == "file"):
                # Top-level locals are not visible inside a type.
                if name in scope.bindings:
                    return scope.bindings[name]
            scope = scope.parent
        return None

    def candidates(self, classify: Classify) -> frozenset[str]:
        """Names some declaration in the file gives a label — the only ones worth a lookup."""
        return frozenset(name for s in self._all for name, bs in s.bindings.items()
                         if any(classify(b) is not None for b in bs))

    def labels_at(self, node: Node, classify: Classify,
                  names: Iterable[str]) -> dict[str, str]:
        """``name → label`` for each of ``names`` that, used at ``node``, binds to a labelled
        declaration."""
        out: dict[str, str] = {}
        for name in names:
            label = label_of(self.lookup(node, name), classify)
            if label is not None:
                out[name] = label
        return out


def label_of(bindings: list[Binding] | None, classify: Classify) -> str | None:
    """The one label every declaration agrees on, else None."""
    if not bindings:
        return None
    labels = {classify(b) for b in bindings}
    return labels.pop() if len(labels) == 1 else None


def receiver_label(receiver: Node, source: bytes, scopes: TypeScopes,
                   classify: Classify) -> str | None:
    """The label of a call's receiver: a name (``_s3``), ``this._s3``, or either in
    parentheses, bound at the call."""
    if receiver.type == "identifier":
        return label_of(scopes.lookup(receiver, node_text(receiver, source)), classify)
    if receiver.type == "member_access_expression":
        obj, name = receiver.child_by_field_name("expression"), receiver.child_by_field_name("name")
        if obj is not None and obj.type in ("this", "this_expression") and name is not None:
            return label_of(scopes.lookup(receiver, node_text(name, source), this_only=True),
                            classify)
        return None
    if receiver.type == "parenthesized_expression" and receiver.named_children:
        return receiver_label(receiver.named_children[0], source, scopes, classify)
    return None
