"""Parse VB source through a length-preserving shadow that joins stacked attribute blocks.

The VB grammar lets a declaration carry only **one** ``attribute_block``. Attributes written
one per line (the common style)::

    <HttpPost>
    <Route("create")>
    Public Function Create() As IActionResult

leave every block but the last in an ``ERROR`` node — the verb is lost, and inside an
interface the error can swallow the whole file. The shadow joins such a run into one block
for the parser only, byte for byte in place (``>`` → ``,`` and ``<`` → space)::

    <HttpPost,
     Route("create")>

so every offset, line and text still comes from the original source. A run is joined only when
each line holds one attribute block and nothing else and the run is directly followed by a
declaration line; ``<Assembly:``/``<Module:`` targets, XML-literal tags (``</x>``, ``<x/>``,
``<x:y>``) and lines with a trailing comment or ``_`` continuation are left alone, so anything
else parses exactly as written.
"""

from __future__ import annotations

import re

from tree_sitter import Tree

from ..treesitter import parse_source

#: One attribute block alone on its line: ``<Name>``, ``<Name(…)>``, ``<A, B(…)>``. The name must
#: not be followed by ``:`` (``<Assembly: X>``, an ``<xs:element>`` XML tag), and the line may hold
#: no closing or self-closing tag and no comment.
_ATTR_LINE = re.compile(rb"^[ \t]*<[ \t]*[A-Za-z_][\w.]*[ \t]*(?![\w.:])[^\n']*>[ \t]*\r?$")
_XML_TAG = re.compile(rb"</|/>")

#: A declaration a stacked attribute run may sit on (VB keywords are case-insensitive).
_DECLARATION = re.compile(
    rb"^[ \t]*(?:Public|Private|Protected|Friend|Shared|Overrides|Overridable|NotOverridable|"
    rb"MustOverride|Overloads|Shadows|ReadOnly|WriteOnly|Default|Partial|MustInherit|"
    rb"NotInheritable|Static|Async|Iterator|Widening|Narrowing|Function|Sub|Property|Class|"
    rb"Interface|Structure|Module|Enum|Event|Delegate|Declare|Const|Dim|WithEvents|Custom|"
    rb"Operator)\b",
    re.IGNORECASE,
)


def _is_attr_line(line: bytes) -> bool:
    return bool(_ATTR_LINE.match(line)) and not _XML_TAG.search(line)


def join_attribute_blocks(source: bytes) -> bytes:
    """The shadow: each run of two or more attribute-only lines directly above a declaration,
    joined into one block in place. Returns ``source`` itself when nothing is joined."""
    if b"<" not in source:
        return source
    lines = source.split(b"\n")
    out: bytearray | None = None
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    i = 0
    while i < len(lines):
        if not _is_attr_line(lines[i]):
            i += 1
            continue
        j = i
        while j < len(lines) and _is_attr_line(lines[j]):
            j += 1
        if j - i >= 2 and j < len(lines) and _DECLARATION.match(lines[j]):
            if out is None:
                out = bytearray(source)
            for k in range(i, j):
                line = lines[k].rstrip(b"\r")
                if k < j - 1:  # close → comma
                    out[offsets[k] + line.rindex(b">")] = ord(",")
                if k > i:  # open → space
                    out[offsets[k] + line.index(b"<")] = ord(" ")
        i = j
    return bytes(out) if out is not None else source


def parse_vb(source: bytes, timeout_micros: int = 0) -> Tree:
    """Parse VB ``source`` through the attribute-joining shadow. Read node text from the
    original ``source`` — the shadow differs only in the joined ``<``/``>`` bytes."""
    return parse_source("vb", join_attribute_blocks(source), timeout_micros)
