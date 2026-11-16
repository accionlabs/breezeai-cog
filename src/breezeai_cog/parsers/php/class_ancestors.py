"""Which classes of the repository extend which — a registered fact, ``php.class-ancestors``.

PHP inheritance crosses files: ``class Song extends BaseModel`` in one file, ``class BaseModel
extends Model`` in another. The fact maps every repository class to its ancestors, nearest
first, fully qualified by PHP's rules (``use`` imports, then the file's namespace)::

    App\\Models\\Song      → (App\\Models\\BaseModel, Illuminate\\Database\\Eloquent\\Model)
    App\\Models\\BaseModel → (Illuminate\\Database\\Eloquent\\Model,)

The chain stops at the first class not declared in the repository (a library's base class), and
at a class declared twice with different parents: what is above it is unknown (such a class has
no ancestors itself). A library detector reads it with :func:`kinds_by_base` to learn which
classes are its models or repositories.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tree_sitter import Node

from ..additive import FactSpec, index_fact, register_fact
from ..treesitter import node_text
from .imports import resolve_class, use_aliases
from .index import namespace_of

FACT = "php.class-ancestors"


def _collect(root: Node, source: bytes, rel: str, repo_root: Any) -> list[tuple[str, str]] | None:
    """``(class, parent)`` fully qualified, for every class of the file that extends one."""
    aliases, namespace = use_aliases(root, source), namespace_of(root, source)
    found: list[tuple[str, str]] = []

    def visit(node: Node) -> None:
        for child in node.named_children:
            if child.type == "class_declaration":
                name = child.child_by_field_name("name")
                base = next((c for c in child.named_children if c.type == "base_clause"), None)
                parent_node = next(iter(base.named_children), None) if base is not None else None
                parent = (resolve_class(node_text(parent_node, source), aliases, namespace)
                          if parent_node is not None else None)
                if name is not None and parent:
                    own = node_text(name, source)
                    found.append((f"{namespace}\\{own}" if namespace else own, parent))
            visit(child)

    visit(root)
    return found or None


def _resolve(values: list[list[tuple[str, str]]], index: Any) -> dict[str, tuple[str, ...]] | None:
    """class → ancestors, nearest first. Order-independent: a class declared twice with
    different parents ends the chain, whatever the file order."""
    parents: dict[str, str | None] = {}
    for file_values in values:
        for cls, parent in file_values:
            parents[cls] = parent if parents.get(cls, parent) == parent else None
    ancestors: dict[str, tuple[str, ...]] = {}
    for cls in parents:
        chain: list[str] = []
        above = parents[cls]
        while above is not None and above != cls and above not in chain:  # not ambiguous, no cycle
            chain.append(above)
            above = parents.get(above)
        if chain:
            ancestors[cls] = tuple(chain)
    return ancestors or None


register_fact(FactSpec(name=FACT, language="php", gate=(b"extends",),
                       collect=_collect, resolve=_resolve))


_kinds_cache: dict[tuple[int, int], dict[str, str]] = {}


def kinds_by_base(index: Any | None, bases: Mapping[str, str]) -> dict[str, str]:
    """Repository class → kind, for each class whose nearest ancestor in ``bases`` (library base
    class → kind) is one. Computed once per index and table."""
    ancestors: dict[str, tuple[str, ...]] | None = index_fact(index, FACT)
    if not ancestors:
        return {}
    key = (id(ancestors), id(bases))
    cached = _kinds_cache.get(key)
    if cached is None:
        cached = {}
        for cls, chain in ancestors.items():
            kind = next((bases[a] for a in chain if a in bases), None)
            if kind is not None:
                cached[cls] = kind
        _kinds_cache.clear()  # one index per process at a time
        _kinds_cache[key] = cached
    return cached
