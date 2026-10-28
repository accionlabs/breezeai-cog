"""Resolver objects as a registered fact, ``graphql.resolver-objects``.

A resolver map's root type can be an object declared in another file and imported by name::

    // queries.ts
    export const queries = {
      books: async () => [],                  // line 2
      book(_, { id }) { return null; },       // line 3
    };
    // server.ts
    import { queries } from './queries';
    const resolvers = { Query: queries };

The detector that reads ``Query: queries`` (:mod:`.resolver_modules`) needs the object's
properties and where each function starts, which only the other file knows. This fact collects
them while the TypeScript index is built: each top-level object whose properties are all
functions, keyed by file and declared name, with per property ``(name, handler, line)`` — the
``handler`` / ``handlerLine`` an in-file resolver map gives (``routes._handler``)::

    ("src/queries.ts", "queries") → (("books", None, 2), ("book", "book", 3))

A property whose function is not written inline (``books: getBooks``, ``books: wrap(fn)``) keeps
neither a name nor a line: that name is declared in this file or imported into it, and the record
that reads the fact lives in another file, so nothing is guessed. An object with a data property
(a literal, an array, a plain object) is no resolver object and is left out.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..typescript.declarations import top_level_declarations
from ..typescript.imports import TsAliasIndex, _pair_key_name, _unwrap_const_expr
from .routes import _handler, _operation_fields

FACT = "graphql.resolver-objects"

#: One property: ``(name, handler, line)`` — as for an in-file resolver map record.
Member = tuple[str, str | None, int | None]
#: ``(file, declared name)`` → the object's properties.
ResolverObjects = dict[tuple[str, str], tuple[Member, ...]]

#: Property values that are (or refer to) a function. A ``{ subscribe }`` object is a function
#: too, but only the one that names ``subscribe`` or ``resolve``.
_FUNCTION_VALUES = frozenset(
    {
        "arrow_function",
        "function_expression",
        "function",
        "identifier",
        "member_expression",
        "call_expression",
    }
)


def resolver_object(index: TsAliasIndex | None, file: str, name: str) -> tuple[Member, ...]:
    """The properties of the resolver object ``name`` declared in ``file`` (``()`` when it is
    unknown or is not one)."""
    objects: ResolverObjects = index_fact(index, FACT) or {}
    return objects.get((file, name), ())


def _property_name(field: Node, source: bytes) -> str | None:
    """A plain property name (``books``, ``'books'``); None for a computed or numeric key."""
    return _pair_key_name(
        field.child_by_field_name("key") or field.child_by_field_name("name"), source
    )


def _is_function(field: Node, source: bytes) -> bool:
    if field.type == "method_definition":
        return True
    value = field.child_by_field_name("value")
    if value is None:
        return False
    if value.type == "object":
        return any(
            _property_name(p, source) in ("subscribe", "resolve")
            for p in value.named_children
            if p.type in ("pair", "method_definition")
        )
    return value.type in _FUNCTION_VALUES


def _members(value: Node, source: bytes) -> tuple[Member, ...]:
    """The properties of an object literal whose every property is a function, else ``()``."""
    obj = _unwrap_const_expr(value)
    fields = _operation_fields(obj) if obj is not None and obj.type == "object" else []
    if not fields or not all(_is_function(f, source) for f in fields):
        return ()
    members: list[Member] = []
    for field in fields:
        name = _property_name(field, source)
        if name is not None:
            handler, line = _handler(field, source, name)
            members.append((name, handler if line is not None else None, line))
    return tuple(members)


def _collect(
    root: Node | None, source: bytes, rel: str, repo_root: Path | None
) -> tuple[str, list[tuple[str, tuple[Member, ...]]]] | None:
    if root is None:
        return None
    found = [
        (name, members)
        for name, (_, value) in top_level_declarations(root, source).items()
        if (members := _members(value, source))
    ]
    return (rel, found) if found else None


def _resolve(
    values: list[tuple[str, list[tuple[str, tuple[Member, ...]]]]], index: object
) -> ResolverObjects | None:
    objects = {(rel, name): members for rel, found in values for name, members in found}
    return objects or None


register_fact(
    FactSpec(
        name=FACT,
        language="typescript",
        gate=(b"export",),
        collect=_collect,
        resolve=_resolve,
    )
)
