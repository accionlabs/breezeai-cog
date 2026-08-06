"""Single-File-Component (``.vue``) script extraction.

A ``.vue`` file is not valid TypeScript — it wraps ``<template>``, ``<script>`` /
``<script setup>``, and ``<style>`` blocks. To reuse the (well-tested) TS grammar and
keep every emitted line/column pointing at the real ``.vue`` position, we build a
**shadow source**: a byte-for-byte copy of the file in which everything outside the
``<script>`` block(s) is blanked to spaces, with newlines preserved. The script bytes
stay at their original offsets, so tree-sitter reports true line/column numbers with no
offset arithmetic.

    <template>            ............        (blanked; newline kept)
      <p>{{ x }}</p>  ->  ...............
    </template>           ...........
    <script setup>        .............
    import { ref } from 'vue'   <-- kept verbatim, at its real line
    </script>             .........

Only ``<script>`` content is captured for now; ``<template>`` bindings (``@click``,
``<router-link>``) are a known gap (honest-null), not a guess.

DESIGN DECISION TO RAISE (skill §2/§4): block-finding here is a minimal tag scan, not a
real grammar. If SFC edge cases (a ``</script>`` inside a script string, exotic block
attributes) prove common on real repos, swap this for the ``tree_sitter_vue`` grammar
via the external-grammar hook in ``treesitter.py`` — same shadow-source output, sturdier
block boundaries. That is a dependency decision, so raise it before adding the grammar."""

from __future__ import annotations

import re

# ``<script>`` / ``<script setup lang="ts">`` … ``</script>``. DOTALL so the body spans
# lines; non-greedy so each block ends at its own closing tag. group("attrs") carries the
# opening-tag attributes (``setup``, ``lang="ts"``); group("body") is the script content.
_SCRIPT_RE = re.compile(
    rb"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
    re.DOTALL | re.IGNORECASE,
)
_NEWLINE = 0x0A
_SPACE = 0x20


def script_ranges(source: bytes) -> list[tuple[int, int]]:
    """Byte ranges (start, end) of every ``<script>`` block **body** in the SFC."""
    return [m.span("body") for m in _SCRIPT_RE.finditer(source)]


def script_grammar(source: bytes) -> str:
    """The tree-sitter grammar for the SFC's script. ``tsx`` only when a block declares a
    JSX-ish lang (``lang="tsx"``/``"jsx"``); ``typescript`` otherwise (Vue script blocks are
    plain TS/JS the overwhelming majority of the time)."""
    for m in _SCRIPT_RE.finditer(source):
        attrs = m.group("attrs").lower()
        if b"lang=" in attrs and (b"tsx" in attrs or b"jsx" in attrs):
            return "tsx"
    return "typescript"


def script_language(source: bytes) -> str:
    """The SFC's source LANGUAGE label — ``typescript`` if any ``<script>`` block declares a
    TS ``lang`` (``lang="ts"`` / ``lang="tsx"``), else ``javascript``. Distinct from
    ``script_grammar`` (which only selects a tree-sitter grammar): this drives
    ``FileRecord.language`` so the JS/TS distinction survives on ``.vue`` files, where the
    ``.vue`` extension can't carry it. A template-only SFC (no ``<script>``) has no code, so it
    defaults to ``javascript`` — the type-free baseline; we never guess TS."""
    for m in _SCRIPT_RE.finditer(source):
        attrs = m.group("attrs").lower()
        if b"lang=" in attrs and b"ts" in attrs:  # matches lang="ts" and lang="tsx"
            return "typescript"
    return "javascript"


def shadow_source(source: bytes, ranges: list[tuple[int, int]]) -> bytes:
    """A same-length copy of ``source`` with everything outside ``ranges`` blanked to
    spaces (newlines preserved), so the TS grammar parses only the script content while
    every node keeps its true ``.vue`` line/column."""
    shadow = bytearray(len(source))
    for i, byte in enumerate(source):
        shadow[i] = byte if byte == _NEWLINE else _SPACE
    for start, end in ranges:
        shadow[start:end] = source[start:end]
    return bytes(shadow)
