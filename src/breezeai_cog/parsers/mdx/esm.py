"""The ESM blocks of an MDX file — its top-level ``import`` / ``export`` code — as a shadow
source a JavaScript grammar can parse.

MDX lets a document hold JavaScript modules code between its Markdown blocks::

    import { Chart } from '../components/Chart'      ← ESM (kept)

    # Sales report
    <Chart values={[1, 2, 3]} />                     ← JSX (blanked)

    ```js
    import { Chart } from '../components/Chart'      ← code sample (blanked)
    ```

A line starts an ESM block only where MDX would read one: at column 0, after a blank line (or
at the top of the document, or after its front matter), outside a fenced code block, a JSX tag,
an open JSX element and a ``{…}`` expression. The block runs to the next blank line; like MDX,
it continues across blank lines while the code so far does not parse
(``export const meta = {`` ⏎⏎ ``title: 'x'`` ⏎ ``}``).

Code shown in a document (a fence, ``code={`…`}``) is never ESM: reading it as an import would
put a false edge in the graph, which is worse than missing one. Where the scan cannot tell (a
quote in JSX text inside an expression), it stays inside the expression, so later lines are
not read as ESM.

Known misses (never false edges): ESM right after a heading, a closing fence or a JSX line with
no blank line between — MDX reads it, this scan needs the blank line — and ESM after a ``{…}``
expression whose JSX text holds a lone quote (``{ok && <p>it's</p>}``).

:func:`esm_shadow` returns a copy of the source of the same byte length with only the ESM
lines kept (everything else spaces, line breaks kept), so node positions are the file's own —
the same technique as the Vue ``<script>`` shadow.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

#: MDX reads ESM only where the keyword is followed by a space (``import{a}`` is text).
_ESM_START = re.compile(rb"(?:import|export) ")
#: MDX turns indented code off, so a fence may be indented any amount (``    ```js`` is a fence).
_FENCE_OPEN = re.compile(rb"[ \t]*(`{3,}|~{3,})(.*)")
_FRONT_MATTER = (b"---",)
_BOM = b"\xef\xbb\xbf"

#: How many further paragraphs an ESM block that does not parse may take in, looking for the
#: end of its code (MDX's continuation rule). A real MDX file never needs many — one that does
#: not parse at all fails to compile — so the bound only keeps a broken file linear.
MAX_CONTINUATION_PARAGRAPHS = 20
#: The most code a continued block may hold. ESM headers are short; a block that would need more
#: is a broken file, and re-parsing large prose would only cost time.
MAX_CONTINUATION_BYTES = 16 * 1024


@dataclass
class _Scan:
    """The Markdown / JSX state carried from one line to the next."""

    #: Open fenced code block: (fence character, its length).
    fence: tuple[int, int] | None = None
    #: Open JavaScript contexts inside a ``{…}`` expression: ``{`` or a template literal `` ` ``.
    stack: list[int] = field(default_factory=list)
    in_comment: bool = False
    #: Inside a JSX tag (between ``<Name`` and ``>``); ``closing`` for ``</Name``.
    in_tag: bool = False
    closing: bool = False
    #: Quote of an open attribute string in a tag.
    attr_quote: int | None = None
    #: Open JSX elements (``<Note>`` … ``</Note>``); ESM is only at the top of the document.
    elements: int = 0
    #: Length of an open inline code span's backtick run.
    code_span: int | None = None
    #: Inside an HTML comment ``<!-- … -->`` (MDX 1 files use them to comment code out).
    html_comment: bool = False

    def at_top(self) -> bool:
        return (
            not self.stack
            and not self.in_tag
            and not self.in_comment
            and self.elements == 0
            and self.code_span is None
            and not self.html_comment
        )

    def in_text(self) -> bool:
        return not self.stack and not self.in_tag and not self.in_comment and not self.html_comment


def _run(line: bytes, i: int, char: int) -> int:
    """Length of the run of ``char`` starting at ``i``."""
    j = i
    while j < len(line) and line[j] == char:
        j += 1
    return j - i


def _scan_expression(s: _Scan, line: bytes, i: int) -> int:
    """Advance through JavaScript inside ``{…}``; returns the index after what was read."""
    top = s.stack[-1]
    c = line[i]
    if s.in_comment:
        if line.startswith(b"*/", i):
            s.in_comment = False
            return i + 2
        return i + 1
    if top == ord("`"):
        if c == ord("\\"):
            return i + 2
        if c == ord("`"):
            s.stack.pop()
        elif line.startswith(b"${", i):
            s.stack.append(ord("{"))
            return i + 2
        return i + 1
    if c in (ord("'"), ord('"')):
        # A string ends on its own line; one left open ends with the line (JSX text such as
        # ``it's`` reads as a string: the expression then stays open — no ESM, never a false one).
        j = i + 1
        while j < len(line) and line[j] != c:
            j += 2 if line[j] == ord("\\") else 1
        return j + 1 if j < len(line) else len(line)
    if line.startswith(b"//", i):
        return len(line)
    if line.startswith(b"/*", i):
        s.in_comment = True
        return i + 2
    # A regex literal is not told apart from division: a ``}`` inside one (``/}/``) closes the
    # expression early. Rare in MDX expressions; accepted.
    if c in (ord("{"), ord("`")):
        s.stack.append(c)
    elif c == ord("}"):
        s.stack.pop()
    return i + 1


def _scan_tag(s: _Scan, line: bytes, i: int) -> int:
    """Advance through a JSX tag's attributes."""
    c = line[i]
    if s.attr_quote is not None:
        if c == s.attr_quote:
            s.attr_quote = None
        return i + 1
    if c in (ord("'"), ord('"')):
        s.attr_quote = c
    elif c == ord("{"):
        s.stack.append(c)
    elif line.startswith(b"/>", i):
        s.in_tag = False
        return i + 2
    elif c == ord(">"):
        s.in_tag = False
        s.elements = max(s.elements - 1, 0) if s.closing else s.elements + 1
    return i + 1


def _scan_text(s: _Scan, line: bytes, i: int) -> int:
    """Advance through Markdown text: code spans, ``{`` expressions and JSX tags."""
    c = line[i]
    if s.code_span is not None:
        n = _run(line, i, ord("`")) if c == ord("`") else 0
        if n == s.code_span:
            s.code_span = None
        return i + max(n, 1)
    if c == ord("\\"):
        return i + 2
    if c == ord("`"):
        n = _run(line, i, c)
        s.code_span = n
        return i + n
    if c == ord("{"):
        s.stack.append(c)
        return i + 1
    if c == ord("<"):
        if line.startswith(b"<!--", i):
            s.html_comment = True
            return i + 4
        nxt = line[i + 1 : i + 2]
        if line.startswith(b"</>", i):  # fragment close
            s.elements = max(s.elements - 1, 0)
            return i + 3
        if nxt == b">":  # fragment open
            s.elements += 1
            return i + 2
        if nxt == b"/" or _starts_name(nxt):
            s.in_tag, s.closing = True, nxt == b"/"
            return i + 2
    return i + 1


def _starts_name(b: bytes) -> bool:
    """Whether a byte can start a JSX tag name: a letter, ``_``, ``$`` or any non-ASCII byte
    (a Unicode letter)."""
    return bool(b) and (b.isalpha() or b in (b"_", b"$") or b[0] >= 0x80)


def _scan_line(s: _Scan, line: bytes) -> None:
    i = 0
    while i < len(line):
        if s.html_comment:
            end = line.find(b"-->", i)
            if end < 0:
                return
            s.html_comment = False
            i = end + 3
        elif s.stack or s.in_comment:
            i = _scan_expression(s, line, i)
        elif s.in_tag:
            i = _scan_tag(s, line, i)
        else:
            i = _scan_text(s, line, i)


def _front_matter_end(lines: list[bytes]) -> int:
    """Index of the first line after a leading ``---`` front matter block; 0 when none."""
    if not lines or lines[0].rstrip() not in _FRONT_MATTER:
        return 0
    for k in range(1, len(lines)):
        if lines[k].rstrip() in (b"---", b"..."):
            return k + 1
    return 0


def _paragraph_end(lines: list[bytes], start: int) -> int:
    """Index of the first blank line at or after ``start`` (or the end)."""
    k = start
    while k < len(lines) and lines[k].strip():
        k += 1
    return k


def _block_end(lines: list[bytes], start: int, parses: Callable[[bytes], bool]) -> tuple[int, bool]:
    """(end (exclusive), parses) of the ESM block starting at ``start``: its paragraph,
    extended over following paragraphs while the code does not parse. When no extension makes
    it parse, the paragraph alone, marked as not code."""
    end = _paragraph_end(lines, start)
    if parses(b"\n".join(lines[start:end])):
        return end, True
    stop = end
    for _ in range(MAX_CONTINUATION_PARAGRAPHS):
        k = stop
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k >= len(lines):
            break
        stop = _paragraph_end(lines, k)
        code = b"\n".join(lines[start:stop])
        if len(code) > MAX_CONTINUATION_BYTES:
            break
        if parses(code):
            return stop, True
    return end, False


def esm_lines(source: bytes, parses: Callable[[bytes], bool]) -> list[tuple[int, int]]:
    """The ESM blocks of an MDX ``source`` as 0-based ``[start, end)`` line ranges.
    ``parses(code)`` says whether a block of code parses without error."""
    lines = source.split(b"\n")
    if lines and lines[0].startswith(_BOM):
        lines[0] = lines[0][len(_BOM) :]
    s = _Scan()
    blocks: list[tuple[int, int]] = []
    k = _front_matter_end(lines)
    prev_blank = True
    while k < len(lines):
        line = lines[k].rstrip(b"\r")
        if s.fence is not None:
            char, length = s.fence
            stripped = line.lstrip(b" \t")
            if _run(stripped, 0, char) >= length and not stripped.lstrip(bytes([char])).strip(b" \t"):
                s.fence = None
            k += 1
            prev_blank = False
            continue
        if not line.strip():
            if s.in_text():
                s.code_span = None  # a blank line ends the paragraph a code span is in
            prev_blank = True
            k += 1
            continue
        if s.in_text():
            m = _FENCE_OPEN.fullmatch(line)
            if m and not (m.group(1)[0] == ord("`") and b"`" in m.group(2)):
                s.fence = (m.group(1)[0], len(m.group(1)))
                s.code_span = None
                k += 1
                prev_blank = False
                continue
        if prev_blank and s.at_top() and _ESM_START.match(line):
            end, ok = _block_end(lines, k, parses)
            if ok:  # a block that never parses is not code (prose, or a broken file)
                blocks.append((k, end))
            k = end
            prev_blank = False
            continue
        _scan_line(s, line)
        prev_blank = False
        k += 1
    return blocks


def esm_shadow(source: bytes, parses: Callable[[bytes], bool]) -> bytes:
    """``source`` with only its ESM lines kept: every other byte but ``\\n`` becomes a space."""
    keep = esm_lines(source, parses)
    return keep_lines(source, keep)


def keep_lines(source: bytes, keep: list[tuple[int, int]]) -> bytes:
    """``source`` with only the lines in ``keep`` (``[start, end)``) left; the rest blanked.
    A leading BOM is blanked too."""
    out = bytearray(b" " * len(source))
    offset = 0
    kept = {n for start, end in keep for n in range(start, end)}
    for n, line in enumerate(source.split(b"\n")):
        if n in kept:
            body = line
            if n == 0 and body.startswith(_BOM):
                body = b"   " + body[len(_BOM) :]
            out[offset : offset + len(line)] = body
        if offset + len(line) < len(source):
            out[offset + len(line)] = ord("\n")
        offset += len(line) + 1
    return bytes(out)


def blank_ranges(source: bytes, ranges: list[tuple[int, int]]) -> bytes:
    """``source`` with the byte ranges blanked (line breaks kept)."""
    out = bytearray(source)
    for start, end in ranges:
        for b in range(start, end):
            if out[b] != ord("\n"):
                out[b] = ord(" ")
    return bytes(out)
