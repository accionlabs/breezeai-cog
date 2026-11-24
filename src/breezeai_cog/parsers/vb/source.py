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

so every offset, line and text still comes from the original source. Blocks written several on a
line (``<HttpGet("{id}")> <Authorize>``), joined with a `` _`` continuation, or in front of the
declaration on its own line are joined the same way (the ``_`` becomes a space). A run is joined
only when its lines hold attribute blocks and nothing else (a `` _`` at the end aside) and it is
directly followed by a declaration; a block ends at the first ``>`` outside strings and
parentheses. ``<Assembly:``/``<Module:`` targets, XML-literal tags (``</x>``, ``<x/>``, ``<x:y>``)
and lines with a trailing comment are left alone, so anything else parses exactly as written.

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

Nor has it a member's ``Implements`` clause: in ``Function Fetch(id As String) As Order Implements
IOrderService.GetOrder`` the ``As`` type falls into an ``ERROR`` node and ``IOrderService.GetOrder``
reads as the return type. The shadow blanks the clause on a ``Function``/``Sub``/``Property``/
``Event`` header line (:func:`blank_implements`); :func:`member_implements` reads it from the
original source — on a header continued over several lines too (`` _``, or a parameter list
broken after a ``,``).

Nor can it read a type argument list with a comma or a nested generic: in
``Function A(a As Dictionary(Of String, Integer), b As String)`` the comma closes the parameter
list (``b`` is lost), and ``(Of String, List(Of Integer))`` adds a parameter named ``List``. The
shadow blanks each ``(Of …)`` group that follows an ``As [New] <Type>`` (parameters, return
types, fields, locals) — :func:`blank_generic_arguments` — so the grammar reads
``a As Dictionary, b As String``; :func:`~.functions.type_text` takes the group back from the
original source. A method's own type parameters (``Function FromAsync(Of T1, T2)(``, read
otherwise as its parameter list) are blanked too; VB records no generics of a method. A type's
generic parameters (``Class Repo(Of K, V)``) and generic expressions (``New List(Of T)()``
without ``As``) parse as written.

Three more forms the grammar lacks are read through the shadow too: an ``Optional`` parameter
(``Optional ByVal n`` has ``ByVal`` read as the name: :func:`blank_optional_modifiers` blanks the
keywords), an operator (``Operator +(…)`` and its body read as fields: :func:`blank_operators`
reads it as a ``Function``), and a ``Declare`` statement. A ``Declare`` is a bodiless header, and
the grammar takes the lines after a bodiless header for its body, swallowing the members that
follow. So the shadow blanks each ``Declare`` statement whole, and :func:`parse_declares` reads
them in a parse of their own: per type, the type's opener and ``End`` lines around its headers
with ``Declare [charset]`` and ``Lib "…" [Alias "…"]`` blanked.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import NamedTuple

from tree_sitter import Node, Tree

from ..treesitter import parse_source

#: An attribute block's opening ``<Name``. The name must not be followed by ``:`` (``<Assembly: X>``,
#: an ``<xs:element>`` XML tag).
_BLOCK_OPEN = re.compile(rb"<[ \t]*[A-Za-z_][\w.]*[ \t]*(?![\w.:])")
_XML_TAG = re.compile(rb"</|/>")
#: What may end a line of attribute blocks: nothing, or a `` _`` line continuation.
_LINE_END = re.compile(rb"(?:_[ \t]*)?\r?$")

_KEYWORDS = (
    rb"(?:Public|Private|Protected|Friend|Shared|Overrides|Overridable|NotOverridable|"
    rb"MustOverride|Overloads|Shadows|ReadOnly|WriteOnly|Default|Partial|MustInherit|"
    rb"NotInheritable|Static|Async|Iterator|Widening|Narrowing|Function|Sub|Property|Class|"
    rb"Interface|Structure|Module|Enum|Event|Delegate|Declare|Const|Dim|WithEvents|Custom|"
    rb"Operator)\b"
)
#: A declaration an attribute run may sit on (VB keywords are case-insensitive).
_DECLARATION = re.compile(_KEYWORDS, re.IGNORECASE)


def _block_close(line: bytes, start: int) -> int | None:
    """Offset of the ``>`` closing the attribute block that opens at ``start`` — outside strings
    and parentheses — or None: no close on the line, a comment, or an XML ``/>``."""
    depth = 0
    in_string = False
    for k in range(start + 1, len(line)):
        ch = line[k]
        if ch == 0x22:  # `"`; a doubled `""` inside a string toggles twice
            in_string = not in_string
        elif in_string:
            continue
        elif ch == 0x27:  # `'` starts a comment
            return None
        elif ch == 0x28:
            depth += 1
        elif ch == 0x29:
            depth -= 1
        elif ch == 0x3E and depth == 0:
            return None if line[k - 1] == 0x2F else k
    return None


def _leading_blocks(line: bytes) -> tuple[list[tuple[int, int]], int]:
    """The attribute blocks ``line`` starts with, as (``<``, ``>``) offsets, and the offset of
    what follows them (spaces skipped). No blocks on a line with an XML closing tag."""
    blocks: list[tuple[int, int]] = []
    k = len(line) - len(line.lstrip(b" \t"))
    if _XML_TAG.search(line):
        return blocks, k
    while _BLOCK_OPEN.match(line, k):
        close = _block_close(line, k)
        if close is None:
            break
        blocks.append((k, close))
        k = close + 1
        while k < len(line) and line[k] in b" \t":
            k += 1
    return blocks, k


def _is_attr_line(line: bytes) -> bool:
    """A line of attribute blocks only, maybe continued with `` _``."""
    blocks, rest = _leading_blocks(line)
    return bool(blocks) and bool(_LINE_END.match(line, rest))


#: A string (``""`` escapes; may span lines), a comment, or a run of non-ASCII bytes (group 1).
_NON_ASCII_SCAN = re.compile(rb'"(?:[^"]|"")*"|\'[^\n]*|\bREM\b[^\n]*|([\x80-\xff]+)', re.IGNORECASE)
#: Unicode categories VB allows in a name: letters, combining marks, digits, connectors.
_NAME_CATEGORIES = ("L", "M", "Nd", "Nl", "Pc")


def ascii_identifiers(source: bytes) -> bytes:
    """``source`` with each non-ASCII character a name may hold (``è`` in ``Après``, ``内部``) turned
    into as many ``x`` as it has bytes, outside strings and comments; ``source`` itself when there
    is none. The grammar's names are ASCII only (``Après`` reads as ``Apr`` and an error), and so
    are the patterns below; the names are read back from the original. Anything else (a byte
    order mark, a quotation mark, a full-width space) is left as written."""
    if source.isascii():
        return source
    out: bytearray | None = None
    for m in _NON_ASCII_SCAN.finditer(source):
        if m.start(1) == -1:
            continue
        pos = m.start(1)
        try:
            chars = m.group(1).decode("utf-8")
        except UnicodeDecodeError:
            continue  # not UTF-8: left as written
        for ch in chars:
            size = len(ch.encode("utf-8"))
            if unicodedata.category(ch).startswith(_NAME_CATEGORIES):
                if out is None:
                    out = bytearray(source)
                out[pos:pos + size] = b"x" * size
            pos += size
    return bytes(out) if out is not None else source


def join_attribute_blocks(source: bytes) -> bytes:
    """The shadow: each run of two or more attribute blocks in front of a declaration — one per
    line, several on a line, joined with `` _``, or on the declaration's own line — joined into
    one block in place. Returns ``source`` itself when nothing is joined."""
    if b"<" not in source:
        return source
    lines = source.split(b"\n")
    out: bytearray | None = None
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    i = 0
    while i < len(lines):
        blocks: list[tuple[int, int]] = []
        continuations: list[int] = []
        j = i
        while j < len(lines) and _is_attr_line(lines[j]):
            found, rest = _leading_blocks(lines[j])
            blocks += [(offsets[j] + a, offsets[j] + b) for a, b in found]
            if lines[j][rest:rest + 1] == b"_":
                continuations.append(offsets[j] + rest)
            j += 1
        if j < len(lines):
            found, rest = _leading_blocks(lines[j])
            if _DECLARATION.match(lines[j], rest):
                blocks += [(offsets[j] + a, offsets[j] + b) for a, b in found]
                if len(blocks) >= 2:
                    if out is None:
                        out = bytearray(source)
                    for (_, close), (open_, _) in zip(blocks, blocks[1:]):
                        out[close] = ord(",")
                        out[open_] = ord(" ")
                    for k in continuations:
                        out[k] = ord(" ")
        i = j + 1  # line j is not an attribute line: it ended the run (or there was none)
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


#: A string (``""`` escapes), a comment, or an ``Optional`` parameter keyword with the ``ByVal`` /
#: ``ByRef`` after it (group 1). The grammar reads ``Optional ByVal n`` with ``ByVal`` as the name
#: and cannot read an attribute before ``Optional`` (``<[In]> Optional b``); it reads both forms
#: without the keywords, a default value (``= 5``) included.
_OPTIONAL_SCAN = re.compile(
    rb'"(?:[^"]|"")*"|\'[^\n]*|\bREM\b[^\n]*|\b(Optional(?:[ \t]+(?:ByVal|ByRef))?)\b',
    re.IGNORECASE,
)


def blank_optional_modifiers(source: bytes) -> bytes:
    """``source`` with each ``Optional`` parameter keyword, and the ``ByVal`` / ``ByRef`` after it,
    blanked in place (``Optional ByVal n`` → ``               n``), outside strings and comments;
    ``source`` itself when there is none. A parameter records neither, so nothing is lost: its
    name, type, attributes and default are read from the original source."""
    if not re.search(rb"(?i)optional", source):
        return source
    out: bytearray | None = None
    for m in _OPTIONAL_SCAN.finditer(source):
        if m.start(1) == -1:
            continue
        if out is None:
            out = bytearray(source)
        out[m.start(1):m.end(1)] = b" " * (m.end(1) - m.start(1))
    return bytes(out) if out is not None else source


#: A string, a comment, an operator header's ``Operator`` keyword (group 1) and its token (group 2:
#: ``+``, ``<>``, ``CType``, ``IsTrue``, …), or ``End Operator`` (group 3: its ``Operator``).
_OPERATOR_SCAN = re.compile(
    rb'"(?:[^"]|"")*"|\'[^\n]*|\bREM\b[^\n]*'
    rb"|\b(Operator)[ \t]+(<<|>>|<>|<=|>=|[-+*/\\^&=<>]|CType|Like|Mod|And|Or|Xor|Not"
    rb"|IsTrue|IsFalse)(?=[ \t]*\()"
    rb"|\bEnd[ \t]+(Operator)\b",
    re.IGNORECASE,
)


def blank_operators(source: bytes) -> bytes:
    """``source`` with each operator declaration read as a function, in place: ``Operator`` →
    ``Function`` (both eight letters), its token → as many ``x`` (``Operator <>(`` →
    ``Function xx(``) and ``End Operator`` → ``End Function``, outside strings and comments;
    ``source`` itself when there is none. The grammar has no operator rule: it reads the header
    and every body line as fields. The name is read back from the original
    (:func:`~.functions.method_name`)."""
    if not re.search(rb"(?i)operator", source):
        return source
    out: bytearray | None = None
    for m in _OPERATOR_SCAN.finditer(source):
        keyword = 1 if m.start(1) != -1 else 3 if m.start(3) != -1 else 0
        if not keyword:
            continue
        if out is None:
            out = bytearray(source)
        out[m.start(keyword):m.end(keyword)] = b"Function"
        if keyword == 1:
            out[m.start(2):m.end(2)] = b"x" * (m.end(2) - m.start(2))
    return bytes(out) if out is not None else source


#: A string, a comment, or a ``Declare`` statement's header: ``Declare`` with its charset (group 1),
#: then ``Function`` / ``Sub`` and the name, then ``Lib "…"`` with an optional ``Alias "…"``
#: (group 2).
_DECLARE_SCAN = re.compile(
    rb'"(?:[^"]|"")*"|\'[^\n]*|\bREM\b[^\n]*'
    rb"|\b(Declare(?:[ \t]+(?:Ansi|Unicode|Auto))?)[ \t]+(?:Function|Sub)[ \t]+[\w\[\]]+"
    rb'([ \t]+Lib[ \t]+"[^"\n]*"(?:[ \t]+Alias[ \t]+"[^"\n]*")?)',
    re.IGNORECASE,
)


class DeclareSpan(NamedTuple):
    """One ``Declare`` statement: its lines (``start``..``end``, the attribute lines right above it
    included) and the keyword spans its parse blanks (``Declare [charset]``, ``Lib … [Alias …]``)."""

    start: int
    end: int
    blanks: tuple[tuple[int, int], tuple[int, int]]


def _header_end(source: bytes, pos: int) -> int:
    """The end of the header line that ``pos`` is in: past its parameter list's ``)`` (the list
    may run over several lines) and any `` _`` continuation."""
    depth = 0
    in_string = False
    i = pos
    while i < len(source):
        ch = source[i]
        if ch == 0x22:
            in_string = not in_string
        elif in_string:
            pass
        elif ch == 0x27:  # a comment runs to the end of its line
            nl = source.find(b"\n", i)
            i = len(source) if nl == -1 else nl
            continue
        elif ch == 0x28:
            depth += 1
        elif ch == 0x29:
            depth -= 1
        elif ch == 0x0A and depth <= 0 and not _CONTINUES.search(
                source, source.rfind(b"\n", 0, i) + 1, i):
            return i
        i += 1
    return len(source)


def declare_spans(source: bytes) -> list[DeclareSpan]:
    """Every ``Declare`` statement outside strings and comments, with its header's continuation
    lines (`` _``, or a line ending in ``,`` / ``(``)."""
    if not re.search(rb"(?i)declare", source):
        return []
    spans: list[DeclareSpan] = []
    for m in _DECLARE_SCAN.finditer(source):
        if m.start(1) == -1:
            continue
        start = source.rfind(b"\n", 0, m.start(1)) + 1
        while start > 0:
            above = source.rfind(b"\n", 0, start - 1) + 1
            if not _is_attr_line(source[above:start - 1]):
                break
            start = above
        end = _header_end(source, m.end(2))
        spans.append(DeclareSpan(start, end, ((m.start(1), m.end(1)), (m.start(2), m.end(2)))))
    return spans


#: One implemented member, as written: ``IOrders.Get``, ``IRepository(Of T).GetAll``.
_OF_ARGS = rb"[ \t]*\([ \t]*Of\b(?:[^()\n]|\([^()\n]*\))*\)"  # ``(Of K, List(Of V))``: one nested level
_MEMBER = rb"[\w\[\]]+(?:" + _OF_ARGS + rb")?(?:\.[\w\[\]]+(?:" + _OF_ARGS + rb")?)*"
#: A line break inside a header the next line continues: after an explicit `` _``, or (implicit
#: continuation) after a ``,`` or ``(`` in the parameter list.
_CONTINUED_LINE = rb"(?:[ \t]_|[,(])[ \t]*\r?\n"
#: A member header line ending with an ``Implements A.B[, C.D]`` clause (group 1; group 2 the list);
#: a generic interface is named with its arguments (``IRepository(Of T).GetAll``).
#: The header may start with a joined attribute block and modifiers; a ``'`` before the clause
#: (a string or a comment) or a continuation keeps the line out.
_IMPLEMENTS_HEADER = re.compile(
    rb"^[ \t]*(?:<[^\n]*>[ \t]*)?(?:[A-Za-z]+[ \t]+)*?(?:Function|Sub|Property|Event)[ \t]+"
    rb"(?:[^\n'\"]|" + _CONTINUED_LINE + rb")*?"
    rb"([ \t]Implements[ \t]+(" + _MEMBER + rb"(?:[ \t]*,[ \t]*" + _MEMBER + rb")*))"
    rb"[ \t]*(?:'[^\n]*)?\r?$",
    re.IGNORECASE | re.MULTILINE,
)


def blank_implements(source: bytes) -> bytes:
    """``source`` with each member header's ``Implements …`` clause blanked (spaces); ``source``
    itself when there is none."""
    if not re.search(rb"(?i)implements", source):
        return source
    out: bytearray | None = None
    for m in _IMPLEMENTS_HEADER.finditer(source):
        if out is None:
            out = bytearray(source)
        out[m.start(1):m.end(1)] = b" " * (m.end(1) - m.start(1))
        # A clause on its own continued line: the `` _`` before it would now continue onto a
        # blank line — blank it too.
        j = m.start(1)
        while j > 0 and source[j - 1] in b" \t\r\n":
            j -= 1
        if source[j - 1:j] == b"_" and b"\n" in source[j:m.start(1)]:
            out[j - 1] = 0x20
    return bytes(out) if out is not None else source


#: A string, a comment, an ``As [New] <Type>`` directly followed by a ``(Of`` group (its ``(`` is
#: group 1), or a ``Function``/``Sub`` name directly followed by its type parameters (group 2:
#: ``Function FromAsync(Of T1, T2)(…)`` — read otherwise as the parameter list). The type may be
#: qualified or escaped (``System.Collections.Generic.List``, ``[Of]``).
_AS_GENERIC_SCAN = re.compile(
    rb'"(?:[^"]|"")*"|\'[^\n]*|\bREM\b[^\n]*'
    rb"|(?<![\w.])As[ \t]+(?:New[ \t]+)?[\w.\[\]]+[ \t]*(\()(?=[ \t]*Of\b)"
    rb"|(?<![\w.])(?:Function|Sub)[ \t]+(?:\[[^\]\n]+\]|[A-Za-z_]\w*)[ \t]*(\()(?=[ \t]*Of\b)",
    re.IGNORECASE,
)
#: A further generic segment of a qualified type: ``.Inner(Of B)`` after ``Outer(Of A)``; the
#: ``(`` is group 1.
_NEXT_GENERIC_SEGMENT = re.compile(rb"\.[\w\[\]]+[ \t]*(\()(?=[ \t]*Of\b)", re.IGNORECASE)
#: What ends a line that VB continues onto the next one inside a type argument list: an explicit
#: ``_``, or (implicit continuation) a ``,`` or ``(``.
_CONTINUES = re.compile(rb"(?:[ \t]_|[,(])[ \t]*\r?$")


def generic_group_end(source: bytes, start: int) -> int | None:
    """The offset just past the ``)`` that closes the bracket at ``source[start]``, or None when
    the group is not closed on its logical line (a line ends without a continuation) — one
    forward scan, so the cost is the group's own length."""
    depth, i, n = 0, start, len(source)
    while i < n:
        ch = source[i]
        if ch == 0x28:  # (
            depth += 1
        elif ch == 0x29:  # )
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch == 0x0A and not _CONTINUES.search(source, source.rfind(b"\n", 0, i) + 1, i):
            return None
        i += 1
    return None


def blank_generic_arguments(source: bytes) -> bytes:
    """``source`` with each type argument list after an ``As [New] <Type>`` blanked in place
    (spaces, newlines kept) — ``As Dictionary(Of String, Integer)`` parses as
    ``As Dictionary``; ``source`` itself when there is none. A group not closed on its logical
    line is left as written."""
    if not re.search(rb"(?i)\(\s*Of\b", source):
        return source
    out: bytearray | None = None
    for m in _AS_GENERIC_SCAN.finditer(source):
        start = m.start(1) if m.start(1) != -1 else m.start(2)
        while start != -1:
            end = generic_group_end(source, start)
            if end is None:
                break
            # A group written over several lines stays one logical line for the grammar: a
            # `` _`` continuation before each newline in it (the line break itself is kept).
            breaks = [i - (source[i - 1] == 0x0D) for i in range(start, end) if source[i] == 0x0A]
            if any(b - 2 < start for b in breaks):
                break  # no room for `` _`` (``(`` alone on its line): leave it as written
            if out is None:
                out = bytearray(source)
            _blank(out, start, end)
            for b in breaks:
                out[b - 1] = ord("_")
            nxt = _NEXT_GENERIC_SEGMENT.match(source, end)
            start = nxt.start(1) if nxt is not None else -1
    return bytes(out) if out is not None else source


def _split_members(clause: bytes) -> list[bytes]:
    """``IA.X, IRepo(Of K, V).Y`` → its members, at the commas outside ``(Of …)``."""
    parts, depth, start = [], 0, 0
    for i, ch in enumerate(clause):
        depth += (ch == ord("(")) - (ch == ord(")"))
        if ch == ord(",") and depth == 0:
            parts.append(clause[start:i])
            start = i + 1
    return [*parts, clause[start:]]


def member_implements(source: bytes) -> dict[int, list[str]]:
    """Header line (1-based) → the interface members its ``Implements`` clause names, as
    written (``["IOrderService.GetOrder"]``)."""
    if not re.search(rb"(?i)implements", source):
        return {}
    text = ascii_identifiers(source)  # matched there, read from the original (same offsets)
    return {
        source.count(b"\n", 0, m.start()) + 1:
            [p.strip().decode("utf-8", "replace")
             for p in _split_members(source[m.start(2):m.end(2)])]
        for m in _IMPLEMENTS_HEADER.finditer(text)
    }


#: A type declaration line: optional modifiers, the type keyword, the name (matched after any
#: attribute blocks the line starts with, ``<Serializable> Public Class Inner``).
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


def _type_blocks(source: bytes, original: bytes | None = None) -> list[TypeBlock] | None:
    """Every type declaration, or None when an opener and its ``End`` do not pair up. ``source``
    is matched (names made ASCII, :func:`ascii_identifiers`); a name is read from ``original``."""
    original = source if original is None else original
    lines = source.split(b"\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    stack: list[tuple[bytes, int, str]] = []  # (keyword, start offset, dotted name)
    blocks: list[TypeBlock] = []
    for i, line in enumerate(lines):
        lead = _leading_blocks(line)[1]
        if m := _TYPE_OPEN.match(line[lead:]):
            first = i
            while first > 0 and (_is_attr_line(lines[first - 1])
                                 or _COMMENT_LINE.match(lines[first - 1])):
                first -= 1
            group = 2 if m.group(2) is not None else 3
            at = offsets[i] + lead
            own = original[at + m.start(group):at + m.end(group)].decode("utf-8", "replace").strip()
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
    joined: bytes  # the attribute-joined source, ``Await`` blanked, ``Declare`` statements blanked
    nested: list[TypeBlock]  # the types inside another type (empty: nothing to do)
    with_declares: bytes  # ``joined`` before its ``Declare`` statements were blanked
    declares: list[DeclareSpan]
    types: list[TypeBlock]  # every type


@lru_cache(maxsize=8)
def _layout(source: bytes) -> _Layout:
    text = ascii_identifiers(source)
    joined = blank_generic_arguments(blank_implements(blank_operators(
        blank_optional_modifiers(blank_await(join_attribute_blocks(text))))))
    # Types are found before the shadows: a joined attribute line no longer ends in `>`, and a
    # nested type must be blanked from its first attribute line on (the shadows keep every line).
    blocks = _type_blocks(text, source)
    # A `Declare` is read by its own parse (parse_declares): the grammar has no `Declare`, and the
    # bodiless header it would see takes the lines after it for a body.
    declares = declare_spans(text)
    if declares:
        out = bytearray(joined)
        for d in declares:
            _blank(out, d.start, d.end)
        return _Layout(bytes(out), [b for b in blocks or [] if b.depth > 0], joined, declares,
                       blocks or [])
    return _Layout(joined, [b for b in blocks or [] if b.depth > 0], joined, [], blocks or [])


def shadow(source: bytes) -> bytes:
    """The text the grammar parsed for ``source`` (same length; nested types not blanked) — for
    a check that must see what the grammar saw, not the original."""
    return _layout(source).joined


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


class DeclareGroup(NamedTuple):
    """The parse of one type's ``Declare`` statements: the type's opener and ``End`` lines and its
    ``Declare`` headers (keywords blanked), everything else blank, at their real offsets."""

    name: str  # the type's dotted name
    line: int  # its opener line (1-based): the one Class record it is, whatever its name
    tree: Tree
    text: bytes  # what the grammar parsed


def _opener_line(source: bytes, block: TypeBlock) -> tuple[int, int]:
    pos = block.start
    while pos < block.end:
        nl = source.find(b"\n", pos)
        nl = block.end if nl == -1 or nl > block.end else nl
        line = source[pos:nl]
        if _TYPE_OPEN.match(line[_leading_blocks(line)[1]:]):
            return pos, nl
        pos = nl + 1
    return block.start, block.start


def parse_declares(source: bytes, timeout_micros: int = 0) -> list[DeclareGroup]:
    """One parse per type holding ``Declare`` statements: the type's opener and ``End`` lines
    around its headers, each read as a bodiless ``Function`` / ``Sub`` (``Declare [charset]`` and
    ``Lib "…" [Alias "…"]`` blanked). Empty when the file has none, or a ``Declare`` is in no type."""
    layout = _layout(source)
    groups: dict[TypeBlock, list[DeclareSpan]] = {}
    for d in layout.declares:
        owner = max((b for b in layout.types if b.start <= d.start and d.end <= b.end),
                    key=lambda b: b.start, default=None)
        if owner is not None:
            groups.setdefault(owner, []).append(d)
    out: list[DeclareGroup] = []
    for block, spans in groups.items():
        text = bytearray(re.sub(rb"[^\r\n]", b" ", source))
        open_start, open_end = _opener_line(ascii_identifiers(source), block)
        end_start = source.rfind(b"\n", 0, block.end) + 1
        for a, b in ((open_start, open_end), (end_start, block.end),
                     *((d.start, d.end) for d in spans)):
            text[a:b] = layout.with_declares[a:b]
        for d in spans:
            for a, b in d.blanks:
                _blank(text, a, b)
        out.append(DeclareGroup(block.name, source.count(b"\n", 0, open_start) + 1,
                                parse_source("vb", bytes(text), timeout_micros), bytes(text)))
    return out
