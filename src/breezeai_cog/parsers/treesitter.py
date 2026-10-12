"""Shared tree-sitter helpers (grammar loading + node utilities).

Note: ``tree_sitter_language_pack.get_parser`` is broken under tree-sitter 0.25
(``parse`` rejects bytes), so we build ``Parser(get_language(name))`` ourselves.
Parsers are cached per language per process (warmed in the pool initializer).

Most grammars come from ``tree_sitter_language_pack``. A few languages need a
standalone PyPI grammar because the language-pack build is unusable — those are
registered in ``_EXTERNAL_GRAMMARS`` and loaded from their own module. Groovy is the
first: the language-pack ``groovy`` grammar is a structureless "command soup" (no
``class``/``method``/``import`` nodes), so we use ``dekobon-tree-sitter-groovy``, a
real structural grammar. See ``.todo/groovy-grammar-evaluation.md``.
"""

from __future__ import annotations

from collections.abc import Collection

import warnings
from functools import lru_cache
from typing import Callable

from tree_sitter import Language, Node, Parser, Tree
from tree_sitter_language_pack import get_language


def _load_dekobon_groovy() -> Language:
    """Load the Groovy grammar from ``dekobon_tree_sitter_groovy``.

    ``pyproject.toml`` pins this distribution to the Accionlabs fork
    (github.com/accionlabs/tree-sitter-groovy), which adds nested-type,
    modifier-enum, enum-members-without-``;``, and enhanced-for support that stock
    dekobon lacks. The upstream PR is pending; the pin moves back to a released
    dekobon version once it merges. See ``.todo/groovy-grammar-evaluation.md``.
    """
    import dekobon_tree_sitter_groovy

    return Language(dekobon_tree_sitter_groovy.language())


#: Languages whose grammar is a standalone PyPI package, not in the language pack.
#: name → zero-arg loader returning a ``tree_sitter.Language``.
_EXTERNAL_GRAMMARS: dict[str, Callable[[], Language]] = {
    "groovy": _load_dekobon_groovy,
}


@lru_cache(maxsize=None)
def get_parser(language: str) -> Parser:
    loader = _EXTERNAL_GRAMMARS.get(language)
    lang = loader() if loader is not None else get_language(language)
    return Parser(lang)


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


#: Cap for a route-table entry's one-line text.
ENTRY_TEXT_LIMIT = 240


def entry_text(node: Node, source: bytes, nested: Collection[str] = ("children",)) -> str:
    """One-line text of a route-table entry (``{ path: 'x', component: X, … }``): whitespace runs
    collapsed to one space, comments dropped (on one line a ``//`` would swallow the code after
    it), the value of a ``nested`` key (child routes, recorded on their own) shown as ``[…]``,
    capped at :data:`ENTRY_TEXT_LIMIT`. The first line of a multi-line object literal is just
    ``{`` — useless as search text."""
    cuts: list[tuple[int, int, bytes]] = []  # (start, end, replacement) byte ranges in source

    def walk(n: Node) -> None:
        if n.type == "comment":
            cuts.append((n.start_byte, n.end_byte, b" "))
            return
        if n.type == "pair" and n.parent == node:
            key, value = n.child_by_field_name("key"), n.child_by_field_name("value")
            if (key is not None and value is not None
                    and node_text(key, source).strip("'\"") in nested):
                cuts.append((value.start_byte, value.end_byte, "[\u2026]".encode()))
                return
        for c in n.children:
            walk(c)

    walk(node)
    raw, pos = b"", node.start_byte
    for s, e, rep in sorted(cuts):
        raw += source[pos:s] + rep
        pos = e
    raw += source[pos:node.end_byte]
    one = " ".join(raw.decode("utf-8", "replace").split())
    return one if len(one) <= ENTRY_TEXT_LIMIT else one[:ENTRY_TEXT_LIMIT - 1] + "\u2026"
