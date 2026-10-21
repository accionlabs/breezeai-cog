"""C# conditional compilation blocks, opened for the declaration walks.

``#if`` / ``#elif`` / ``#else`` wrap declarations in ``preproc_if`` / ``preproc_elif`` /
``preproc_else`` nodes (the ``#elif`` and ``#else`` nested inside the ``#if``)::

    #if DEBUG                       compilation_unit
    namespace Tests {                 preproc_if
        class MarkovNameGenerator …     namespace_declaration …
    }
    #endif

A walk over a node's ``named_children`` would see only the ``preproc_if`` and miss every
declaration inside it. :func:`members` yields the declarations of every branch: all of them are
source code, whichever symbols a build defines.
"""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

_BLOCKS = frozenset({"preproc_if", "preproc_elif", "preproc_else"})


def members(node: Node) -> Iterator[Node]:
    """``node``'s named children, with conditional-compilation blocks opened (recursively)."""
    for child in node.named_children:
        if child.type in _BLOCKS:
            yield from members(child)
        else:
            yield child
