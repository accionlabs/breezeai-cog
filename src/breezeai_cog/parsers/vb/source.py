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

The grammar has no rule for a type inside a type either: ``Public Class Dto`` inside a class reads
as a field, the outer class closes at the nested ``End Class``, and the outer type's members after
it fall into an ``ERROR`` node. :func:`parse_vb` therefore parses the file with every nested type
blanked (spaces, newlines kept — the outer type is whole again), and :func:`parse_nested` parses
each nesting level on its own from a shadow that keeps only that level's types. A line scan finds
the types (``[modifiers] Class|Structure|Interface|Module|Enum Name`` … ``End …``); a file whose
openers and ``End`` lines do not pair up is parsed as written.

The grammar has no ``Await`` either: ``Await _s3.PutObjectAsync(req)`` reads as a member access on
a variable named ``Await`` with ``_s3`` in an ``ERROR`` node. The shadow replaces the keyword
(:func:`blank_await`) so the awaited expression parses as written: with ``Call `` where it starts a
statement (the statement keeps its start, so its id and text are unchanged), with spaces inside an
expression (``Dim r = Await …``, ``Return Await …``). ``Await`` is replaced only
outside strings and comments, when followed by whitespace and the start of a name — never before
an operator keyword (``Await Is Nothing``), ``=``, ``.``, ``(`` or ``,``, where it can be a variable
(VB reserves it only inside ``Async`` methods).
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import NamedTuple

from tree_sitter import Node, Tree

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


#: A string (``""`` escapes; may span lines), a comment, or an ``Await`` keyword (group 1).
_AWAIT_SCAN = re.compile(
    rb'"(?:[^"]|"")*"|\'[^\n]*|\bREM\b[^\n]*'
    rb"|(?<![\w.])(Await)(?=[ \t]+(?!(?:Is|IsNot|And|AndAlso|Or|OrElse|Xor|Mod|Like|As|To|Then"
    rb"|Step)\b)[A-Za-z_\[])",
    re.IGNORECASE,
)


#: What precedes an ``Await`` that starts a statement, on its line.
_STATEMENT_START = re.compile(rb"(?:^|:|\bThen|\bElse)[ \t]*$", re.IGNORECASE)


def blank_await(source: bytes) -> bytes:
    """``source`` with each ``Await`` keyword replaced in place — ``Call `` at a statement's start,
    five spaces inside an expression; ``source`` itself when there is none."""
    if not re.search(rb"(?i)await", source):
        return source
    out: bytearray | None = None
    for m in _AWAIT_SCAN.finditer(source):
        start = m.start(1)
        if start == -1:
            continue
        if out is None:
            out = bytearray(source)
        line = source[source.rfind(b"\n", 0, start) + 1:start]
        out[start:m.end(1)] = b"Call " if _STATEMENT_START.search(line) else b"     "
    return bytes(out) if out is not None else source


#: A type declaration line: optional modifiers, the type keyword, the name.
_TYPE_OPEN = re.compile(
    rb"^[ \t]*(?:(?:Public|Private|Protected|Friend|Shared|Partial|NotInheritable|MustInherit|"
    rb"Shadows|Overloads)[ \t]+)*(Class|Structure|Interface|Module|Enum)[ \t]+"
    rb"(?:\[([^\]\r\n]+)\]|([A-Za-z_]\w*))",
    re.IGNORECASE,
)
_TYPE_CLOSE = re.compile(rb"^[ \t]*End[ \t]+(Class|Structure|Interface|Module|Enum)\b", re.IGNORECASE)
#: A comment line (``'`` / ``REM``) — a nested type's doc comment goes with it.
_COMMENT_LINE = re.compile(rb"^[ \t]*(?:'|REM\b)", re.IGNORECASE)


class TypeBlock(NamedTuple):
    """One type declaration's bytes (with the attribute and comment lines right above it),
    its nesting depth (0 = not inside another type) and its dotted name (``Outer.Inner``)."""

    start: int
    end: int
    depth: int
    name: str


def _type_blocks(source: bytes) -> list[TypeBlock] | None:
    """Every type declaration, or None when an opener and its ``End`` do not pair up."""
    lines = source.split(b"\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    stack: list[tuple[bytes, int, str]] = []  # (keyword, start offset, dotted name)
    blocks: list[TypeBlock] = []
    for i, line in enumerate(lines):
        if m := _TYPE_OPEN.match(line):
            first = i
            while first > 0 and (_is_attr_line(lines[first - 1])
                                 or _COMMENT_LINE.match(lines[first - 1])):
                first -= 1
            own = (m.group(2) or m.group(3)).decode("utf-8", "replace").strip()
            name = f"{stack[-1][2]}.{own}" if stack else own
            stack.append((m.group(1).lower(), offsets[first], name))
        elif m := _TYPE_CLOSE.match(line):
            if not stack or stack[-1][0] != m.group(1).lower():
                return None
            _, start, name = stack.pop()
            blocks.append(TypeBlock(start, offsets[i] + len(line.rstrip(b"\r")), len(stack), name))
    return None if stack else blocks


def _blank(out: bytearray, start: int, end: int) -> None:
    for k in range(start, end):
        if out[k] not in (0x0A, 0x0D):
            out[k] = 0x20


class _Layout(NamedTuple):
    joined: bytes  # the attribute-joined source, ``Await`` blanked
    nested: list[TypeBlock]  # the types inside another type (empty: nothing to do)


@lru_cache(maxsize=8)
def _layout(source: bytes) -> _Layout:
    joined = blank_await(join_attribute_blocks(source))
    blocks = _type_blocks(joined)
    return _Layout(joined, [b for b in blocks or [] if b.depth > 0])


def _level_shadow(layout: _Layout, depth: int) -> bytes:
    """The joined source with everything but the depth-``depth`` types blanked (and the types
    nested in those); depth 0 keeps everything but the nested types."""
    out = bytearray(layout.joined)
    if depth == 0:
        for b in layout.nested:
            if b.depth == 1:
                _blank(out, b.start, b.end)
        return bytes(out)
    keep = sorted((b.start, b.end) for b in layout.nested if b.depth == depth)
    pos = 0
    for start, end in keep:
        _blank(out, pos, start)
        pos = end
    _blank(out, pos, len(out))
    for b in layout.nested:
        if b.depth == depth + 1:
            _blank(out, b.start, b.end)
    return bytes(out)


def parse_vb(source: bytes, timeout_micros: int = 0) -> Tree:
    """Parse VB ``source`` through the shadow: stacked attribute blocks joined, and any type
    nested in another type blanked (see :func:`parse_nested` for those). Read node text from
    the original ``source`` — the shadow differs only in joined ``<``/``>`` and blanked bytes."""
    layout = _layout(source)
    shadow = _level_shadow(layout, 0) if layout.nested else layout.joined
    return parse_source("vb", shadow, timeout_micros)


class NestedLevel(NamedTuple):
    """The parse of one nesting level: its types are the tree's top-level types."""

    root: Node
    blocks: list[TypeBlock]

    def owner(self, node: Node) -> str | None:
        """The dotted name of the type ``node``'s type is declared in (``Outer`` for
        ``Outer.Inner``)."""
        for b in self.blocks:
            if b.start <= node.start_byte < b.end:
                return b.name.rsplit(".", 1)[0]
        return None


def parse_nested(source: bytes, timeout_micros: int = 0) -> list[NestedLevel]:
    """One parse per nesting level of the types declared inside another type, at their real
    offsets; empty when the file has none."""
    layout = _layout(source)
    levels: list[NestedLevel] = []
    for depth in sorted({b.depth for b in layout.nested}):
        tree = parse_source("vb", _level_shadow(layout, depth), timeout_micros)
        levels.append(NestedLevel(tree.root_node,
                                  [b for b in layout.nested if b.depth == depth]))
    return levels
