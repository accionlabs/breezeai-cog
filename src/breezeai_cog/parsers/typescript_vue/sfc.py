"""Single-File-Component (``.vue``) blocks, found with the ``vue`` grammar.

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

The blocks are the top-level ``script_element`` / ``template_element`` nodes of the ``vue``
grammar (in ``tree_sitter_language_pack``), not a tag scan: a ``<script>`` inside an HTML
comment is not a block. :func:`template_markup` is the same idea for the ``<template>``
markup, which the shared markup reader reads for links and form posts.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node

from ..template_common.script_blocks import SRC_VALUE, blank


@dataclass(frozen=True, slots=True)
class ScriptBlock:
    """One ``<script>`` block: its body's byte range and its open tag's text (lower case)."""

    start: int
    end: int
    attrs: bytes


def _child(node: Node, kind: str) -> Node | None:
    return next((c for c in node.named_children if c.type == kind), None)


def script_blocks(root: Node) -> list[ScriptBlock]:
    """Every top-level ``<script>`` block of the SFC, in file order (an empty body included)."""
    blocks: list[ScriptBlock] = []
    for node in root.named_children:
        if node.type != "script_element":
            continue
        open_tag, body = _child(node, "start_tag"), _child(node, "raw_text")
        if open_tag is None:
            continue
        if body is not None:
            start, end = body.start_byte, body.end_byte
        else:  # no raw_text: the body is what lies between the open and the close tag
            close = _child(node, "end_tag")
            start = open_tag.end_byte
            end = close.start_byte if close is not None else node.end_byte
        attrs = open_tag.text.lower() if open_tag.text is not None else b""
        blocks.append(ScriptBlock(start, end, attrs))
    return blocks


def script_grammar(blocks: list[ScriptBlock]) -> str:
    """The tree-sitter grammar for the SFC's script. ``tsx`` only when a block declares a
    JSX-ish lang (``lang="tsx"``/``"jsx"``); ``typescript`` otherwise (Vue script blocks are
    plain TS/JS the overwhelming majority of the time)."""
    for b in blocks:
        if b"lang=" in b.attrs and (b"tsx" in b.attrs or b"jsx" in b.attrs):
            return "tsx"
    return "typescript"


def script_language(blocks: list[ScriptBlock]) -> str:
    """The SFC's source LANGUAGE label — ``typescript`` if any ``<script>`` block declares a
    TS ``lang`` (``lang="ts"`` / ``lang="tsx"``), else ``javascript``. Distinct from
    ``script_grammar`` (which only selects a tree-sitter grammar): this drives
    ``FileRecord.language`` so the JS/TS distinction survives on ``.vue`` files, where the
    ``.vue`` extension can't carry it. A template-only SFC (no ``<script>``) has no code, so it
    defaults to ``javascript`` — the type-free baseline; we never guess TS."""
    for b in blocks:
        if b"lang=" in b.attrs and b"ts" in b.attrs:  # matches lang="ts" and lang="tsx"
            return "typescript"
    return "javascript"


def shadow_source(source: bytes, blocks: list[ScriptBlock]) -> bytes:
    """A same-length copy of ``source`` with everything outside the script bodies blanked to
    spaces (newlines preserved), so the TS grammar parses only the script content while
    every node keeps its true ``.vue`` line/column."""
    shadow = bytearray(source)
    blank(shadow, 0, len(shadow))
    for b in blocks:
        shadow[b.start : b.end] = source[b.start : b.end]
    return bytes(shadow)


def template_markup(root: Node, source: bytes) -> bytes | None:
    """A same-length copy of ``source`` that keeps only the markup inside the top-level
    ``<template>`` block(s), or ``None`` when the SFC has none. The ``<template>`` tags
    themselves are blanked: the html grammar would read the block as an inert template."""
    kept: list[tuple[int, int]] = []
    for node in root.named_children:
        if node.type != "template_element":
            continue
        open_tag, close = _child(node, "start_tag"), _child(node, "end_tag")
        start = open_tag.end_byte if open_tag is not None else node.start_byte
        end = close.start_byte if close is not None else node.end_byte
        kept.append((start, end))
    if not kept:
        return None
    markup = bytearray(source)
    blank(markup, 0, len(markup))
    for start, end in kept:
        markup[start:end] = source[start:end]
    return bytes(markup)


def template_src(root: Node) -> str | None:
    """The ``src`` of the SFC's top-level ``<template src="…">`` (markup kept in another file),
    as written; None when the template is inline."""
    for node in root.named_children:
        if node.type != "template_element":
            continue
        open_tag = _child(node, "start_tag")
        m = SRC_VALUE.search(open_tag.text) if open_tag is not None and open_tag.text else None
        if m is not None:
            return next(g for g in m.groups() if g is not None).decode("utf-8", "replace")
    return None
