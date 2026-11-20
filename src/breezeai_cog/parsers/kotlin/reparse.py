"""Kotlin parse with a one-line-body workaround for the tree-sitter grammar.

The grammar ends a declaration only at a newline or a ``;``. ``class C { companion object {
const val R = 3 } val l = 10 }`` — a declaration followed by ``}`` or by another declaration
on the same line — parses as an ``ERROR`` node that swallows the enclosing class, so the class
and its members are never seen (the file's other members fall back to the file as parent).

:func:`parse_kotlin` parses as written; if the tree has errors it parses once more with a
``;`` written over the one space or tab that separates such a declaration from the next token.
The rewrite keeps every byte offset and line, so the nodes of whichever tree is returned are
read against the original source unchanged. The rewritten tree is used only when it has fewer
errors, so a file that parsed cleanly, or that the rewrite does not help, is never changed.
"""

from __future__ import annotations

import re

from tree_sitter import Node, Tree

from ..treesitter import parse_source

#: Words that begin a declaration, so one that follows ``}`` on the same line starts a new one.
_DECL_START = (
    r"val|var|fun|class|object|interface|companion|typealias|init|constructor|enum|data|"
    r"sealed|abstract|open|annotation|inner|const|lateinit|override|private|public|"
    r"protected|internal"
)

#: The space or tab (one byte, so offsets hold) to turn into ``;``: before a ``}`` that ends a
#: declaration, or between a ``}`` and the next declaration on the same line.
_TERMINATOR_GAP = re.compile(
    rb"(?<=[^\s{;])[ \t](?=\})|(?<=\})[ \t](?=(?:" + _DECL_START.encode() + rb")\b)"
)


def _error_count(root: Node) -> int:
    """The number of ``ERROR`` and missing nodes under ``root``."""
    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            count += 1
        stack.extend(node.children)
    return count


def parse_kotlin(source: bytes, timeout_micros: int = 0) -> Tree:
    """The tree of a Kotlin file; a one-line body the grammar cannot end is re-read with ``;``
    over its separating space when that leaves fewer errors (module docstring)."""
    tree = parse_source("kotlin", source, timeout_micros)
    if not tree.root_node.has_error:
        return tree
    rewritten = _TERMINATOR_GAP.sub(b";", source)
    if rewritten == source:
        return tree
    retry = parse_source("kotlin", rewritten, timeout_micros)
    if _error_count(retry.root_node) < _error_count(tree.root_node):
        return retry
    return tree
