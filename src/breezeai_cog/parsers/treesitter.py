"""Shared tree-sitter helpers (grammar loading + node utilities).

Note: ``tree_sitter_language_pack.get_parser`` is broken under tree-sitter 0.25
(``parse`` rejects bytes), so we build ``Parser(get_language(name))`` ourselves.
Parsers are cached per language per process (warmed in the pool initializer, M3).
"""

from __future__ import annotations

import warnings
from functools import lru_cache

from tree_sitter import Node, Parser, Tree
from tree_sitter_language_pack import get_language


@lru_cache(maxsize=None)
def get_parser(language: str) -> Parser:
    return Parser(get_language(language))


def parse_source(language: str, source: bytes, timeout_micros: int = 0) -> Tree:
    """Parse bytes with an optional **cross-platform** timeout.

    Uses tree-sitter's native ``timeout_micros`` (a C-library limit, no OS signals
    — so it works on Linux/macOS/Windows). On timeout the binding raises
    ``ValueError`` ("Parsing failed"), which the pipeline's per-file isolation
    catches and turns into a skip. (``progress_callback`` is ignored for bytestring
    input, so it can't be used here.)
    """
    parser = get_parser(language)
    with warnings.catch_warnings():  # timeout_micros is deprecated but is the only bytes-path option
        warnings.simplefilter("ignore", DeprecationWarning)
        parser.timeout_micros = int(timeout_micros) if timeout_micros and timeout_micros > 0 else 0
    return parser.parse(source)


def node_text(node: Node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


def line_span(node: Node) -> tuple[int, int]:
    """1-based (start_line, end_line)."""
    return node.start_point[0] + 1, node.end_point[0] + 1


def first_line(text: str) -> str:
    return text.split("\n", 1)[0]
