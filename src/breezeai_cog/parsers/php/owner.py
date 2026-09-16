"""Helpers for resolving the nearest enclosing owner (function/method/class) of an AST node.

Used by framework route detectors to set the correct ``parentId`` on route Statement
records, so that e.g. a ``Route::get()`` inside ``RouteServiceProvider::map()`` attaches
to that method rather than to the file root.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import class_id, file_id, function_id
from ..treesitter import node_text

# AST node types that represent function/method/class scopes in PHP tree-sitter grammar.
_FUNCTION_TYPES = frozenset({"function_definition", "method_declaration"})
_CLASS_TYPES = frozenset(
    {"class_declaration", "interface_declaration", "trait_declaration", "enum_declaration"}
)
_SCOPE_TYPES = _FUNCTION_TYPES | _CLASS_TYPES


def owner_id_for_node(node: Node, source: bytes, path: str) -> str:
    """Return the ``id`` of the nearest enclosing function/method/class for *node*.

    Walks up through *node*'s ancestors to find the nearest ``function_definition``,
    ``method_declaration``, or ``class_declaration`` (and sibling class types), then
    reconstructs its ID using the same formula as :func:`build_function` /
    :func:`build_class` so the returned string matches the already-registered record.

    Falls back to ``file_id(path)`` when no enclosing scope is found (top-level code).

    Algorithm mirrors the reference implementations in ``typescript_express`` and
    ``java_springboot``, but operates on the live tree-sitter AST rather than on a
    post-hoc line-range scan, which avoids an extra linear pass over ``functions``.
    """
    fid = file_id(path)

    # Collect the ancestor chain up to (and including) the first scope node.
    # We need both the function node AND its enclosing class (if any) to reconstruct
    # the method ID correctly (class_name kwarg in function_id).
    p = getattr(node, "parent", None)
    while p is not None:
        if p.type in _SCOPE_TYPES:
            return _id_for_scope(p, source, path, fid)
        p = getattr(p, "parent", None)

    return fid


def _id_for_scope(scope: Node, source: bytes, path: str, fid: str) -> str:
    """Reconstruct the canonical ID for a function/method/class AST node."""
    name_node = scope.child_by_field_name("name")
    name = node_text(name_node, source) if name_node is not None else ""
    start_line = scope.start_point[0] + 1  # tree-sitter is 0-indexed; our IDs are 1-indexed

    if scope.type in _FUNCTION_TYPES:
        # Determine class_name by walking up to an enclosing class node.
        class_name: str | None = None
        p = getattr(scope, "parent", None)
        while p is not None:
            if p.type in _CLASS_TYPES:
                cn_node = p.child_by_field_name("name")
                class_name = node_text(cn_node, source) if cn_node is not None else None
                break
            if p.type in _FUNCTION_TYPES:
                # Nested function inside another function — no class context.
                break
            p = getattr(p, "parent", None)
        # NOTE: We do NOT call disambiguate() here because we are reconstructing an ID
        # that was already registered by build_function with disambiguate.  The vast
        # majority of files have no disambiguated suffixes; in the rare collision case
        # the parentId will point to the first definition which is the intended behaviour.
        return function_id(path, name or None, start_line, class_name=class_name)

    # class_declaration / interface_declaration / trait_declaration / enum_declaration
    return class_id(path, name)
