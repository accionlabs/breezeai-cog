"""The JSP lexical layer: decoding, scripting elements, directives and EL.

No tree-sitter grammar covers JSP, so this module finds the JSP constructs itself, in one linear
pass. It accepts every JSP version (1.0 through 4.0); no construct is rejected for being
too old or too new:

* **Standard syntax**: ``<%-- --%>`` comments, ``<%@ %>`` directives, ``<%= %>`` expressions,
  ``<%! %>`` declarations, ``<% %>`` scriptlets (1.0).
* **XML syntax**: ``<jsp:scriptlet>``, ``<jsp:expression>``, ``<jsp:declaration>`` and
  ``<jsp:directive.*>`` (1.2, the ``.jspx`` / ``.tagx`` documents of 2.0). Their bodies are XML
  text, so ``&lt;`` and friends and ``<![CDATA[ ]]>`` are decoded before the code is parsed
  (per line, so lines stay true; columns after an escape on its line move left).
* **EL**: ``${ }`` (2.0) and deferred ``#{ }`` (2.1), with nested braces and quoted strings
  (EL 3.0 lambdas, set and map literals), skipping the ``\\${`` escape.

The spec's own escapes keep the scan exact: template text writes a literal ``<%`` as ``<\\%``
and scripting code writes a literal ``%>`` as ``%\\>``, so a plain search for the delimiters
never splits an element. Constructs a container removed or deprecated (``isThreadSafe``,
``<jsp:plugin>``, both gone in 4.0) are still scanned like any other: tolerated, never an error.

The two **shadows** are copies of the source with the same length (newlines kept), so every
offset, line and column taken from a tree parsed over a shadow is true for the original file
(except the column shift of XML escapes noted above):

* :func:`shadow_markup` blanks every scripting element and directive, leaving the markup for
  the ``html`` grammar (the approach of the Web Forms markup pass).
* :func:`shadow_java` keeps only the code of scriptlets, expressions and declarations for the
  ``java`` grammar, which accepts statements and method declarations at the top level. An
  expression ``<%= x %>`` becomes ``  (x);`` so it parses as a statement.
"""

from __future__ import annotations

import codecs
import re
from bisect import bisect_right
from dataclasses import dataclass

#: ``bytes.translate`` table that blanks every byte except a newline.
_BLANK = bytes(0x0A if i == 0x0A else 0x20 for i in range(256))

#: The opening of any scripting element or directive, standard or XML syntax.
_OPEN = re.compile(rb"<%|<jsp:(scriptlet|expression|declaration|directive\.[A-Za-z]+)(?=[\s/>])")
#: An XML-syntax directive's start tag (attributes only, so ``>`` inside a value is safe).
_XML_DIRECTIVE = re.compile(
    rb"""<jsp:directive\.([A-Za-z]+)((?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(/?)>"""
)
#: A directive name at the start of a ``<%@ … %>`` body.
_DIRECTIVE_NAME = re.compile(rb"\s*([A-Za-z]+)")
#: One ``name="value"`` / ``name='value'`` attribute.
_ATTR = re.compile(rb"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
#: XML text escapes and CDATA markers inside an XML-syntax scripting element.
_XML_TEXT = re.compile(rb"&(?:lt|gt|amp|quot|apos);|<!\[CDATA\[|\]\]>")
_XML_CHAR = {b"&lt;": b"<", b"&gt;": b">", b"&amp;": b"&", b"&quot;": b'"', b"&apos;": b"'"}
#: The start of an EL expression (``\\${`` is escaped and skipped by the scanner).
_EL_OPEN = re.compile(rb"[$#]\{")
#: The longest EL expression scanned; an unterminated one stops here, keeping the pass linear.
_EL_MAX = 8192
#: A declared character encoding: ``pageEncoding``, a ``contentType`` charset, the XML prolog.
_DECLARED_ENCODING = re.compile(
    rb"""(?:pageEncoding\s*=\s*["']|charset\s*=\s*|<\?xml[^>]{0,200}?encoding\s*=\s*["'])"""
    rb"""([A-Za-z0-9._:-]+)"""
)

#: Kinds whose body is Java code.
CODE_KINDS = frozenset({"scriptlet", "expression", "declaration"})


@dataclass(frozen=True, slots=True)
class Island:
    """One scripting element, directive or JSP comment.

    ``start``/``end`` cover the whole construct; ``body_start``/``body_end`` its content (the
    code, the directive's name and attributes, or the comment text). ``closed`` is False when
    the file ends before the closing delimiter; ``xml`` marks the XML-syntax form."""

    kind: str  # comment | directive | expression | declaration | scriptlet
    start: int
    end: int
    body_start: int
    body_end: int
    closed: bool
    xml: bool = False
    name: str | None = None  # the directive name (page, include, taglib, tag, …)


def decode_source(raw: bytes) -> bytes:
    """The file as UTF-8 bytes, whatever its encoding.

    Valid UTF-8 is kept as it is (only a BOM is dropped). Otherwise the declared encoding is
    used (``pageEncoding``, a ``contentType`` charset, the XML prolog), then cp1252, then
    Latin-1, which decodes every byte. Legacy pages in ISO-8859-1, windows-1252 or Shift_JIS
    therefore parse instead of failing; UTF-16 is recognised by its BOM or its zero bytes."""
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return _transcode(raw, "utf-16") or raw.decode("latin-1").encode("utf-8")
    elif len(raw) >= 2 and (raw[0] == 0) != (raw[1] == 0):
        utf16 = "utf-16-be" if raw[0] == 0 else "utf-16-le"
        decoded = _transcode(raw, utf16)
        if decoded is not None:
            return decoded
    try:
        raw.decode("utf-8")
        return raw
    except UnicodeDecodeError:
        pass
    declared = _DECLARED_ENCODING.search(raw)
    if declared is not None:
        decoded = _transcode(raw, declared.group(1).decode("ascii"))
        if decoded is not None:
            return decoded
    return _transcode(raw, "cp1252") or raw.decode("latin-1").encode("utf-8")


def _transcode(raw: bytes, encoding: str) -> bytes | None:
    try:
        return raw.decode(encoding).encode("utf-8")
    except (LookupError, UnicodeDecodeError, UnicodeEncodeError):
        return None


def scan_islands(src: bytes) -> list[Island]:
    """Every scripting element, directive and JSP comment, in source order (one pass)."""
    out: list[Island] = []
    pos, n = 0, len(src)
    while (m := _OPEN.search(src, pos)) is not None:
        start = m.start()
        island = _standard(src, start, n) if m.group(1) is None else _xml(src, m, n)
        if island is None:
            pos = m.end()
            continue
        out.append(island)
        pos = max(island.end, start + 2)
    return out


def _standard(src: bytes, start: int, n: int) -> Island:
    if src.startswith(b"<%--", start):
        kind, body, close = "comment", start + 4, b"--%>"
    else:
        marker = src[start + 2:start + 3]
        kind = {b"@": "directive", b"=": "expression", b"!": "declaration"}.get(marker, "scriptlet")
        body, close = (start + 2 if kind == "scriptlet" else start + 3), b"%>"
    stop = src.find(close, body)
    closed = stop != -1
    body_end = stop if closed else n
    name = None
    if kind == "directive":
        found = _DIRECTIVE_NAME.match(src, body)
        name = found.group(1).decode("ascii") if found is not None else None
    return Island(kind, start, body_end + len(close) if closed else n, body, body_end, closed,
                  name=name)


def _xml(src: bytes, m: re.Match[bytes], n: int) -> Island | None:
    tag = m.group(1)
    start = m.start()
    if tag.startswith(b"directive."):
        found = _XML_DIRECTIVE.match(src, start)
        if found is None:
            return None
        end = found.end()
        if not found.group(3):  # <jsp:directive.page …></jsp:directive.page>
            close = b"</jsp:" + tag + b">"
            stop = src.find(close, end)
            if stop != -1:
                end = stop + len(close)
        return Island("directive", start, end, found.start(2), found.end(2), True, xml=True,
                      name=found.group(1).decode("ascii"))
    gt = src.find(b">", m.end())
    if gt == -1:
        return None
    kind = tag.decode("ascii")
    if src[gt - 1:gt] == b"/":  # an empty element: no code
        return Island(kind, start, gt + 1, gt + 1, gt + 1, True, xml=True)
    close = b"</jsp:" + tag + b">"
    stop = src.find(close, gt + 1)
    closed = stop != -1
    body_end = stop if closed else n
    return Island(kind, start, body_end + len(close) if closed else n, gt + 1, body_end, closed,
                  xml=True)


def directive_attrs(src: bytes, island: Island) -> list[tuple[str, str]]:
    """The ``(name, value)`` attributes of a directive, in source order (``import`` may repeat)."""
    body = src[island.body_start:island.body_end]
    return [
        (m.group(1).decode("utf-8", "replace"),
         (m.group(2) if m.group(2) is not None else m.group(3)).decode("utf-8", "replace"))
        for m in _ATTR.finditer(body)
    ]


def shadow_markup(src: bytes, islands: list[Island]) -> bytes:
    """``src`` with every island blanked: the markup the ``html`` grammar parses."""
    out = bytearray(src)
    for i in islands:
        out[i.start:i.end] = src[i.start:i.end].translate(_BLANK)
    return bytes(out)


def shadow_java(src: bytes, islands: list[Island]) -> bytes:
    """Only the code of scriptlets, expressions and declarations, at their true offsets."""
    out = bytearray(src.translate(_BLANK))
    for i in islands:
        if i.kind not in CODE_KINDS:
            continue
        body = src[i.body_start:i.body_end]
        if i.xml:
            body = b"\n".join(_xml_line(line) for line in body.split(b"\n"))
        out[i.body_start:i.body_end] = body
        if i.kind == "expression" and i.closed and i.body_start > i.start:
            # `<%= x %>` → `  (x);` — the opener's last byte and the closer's first two.
            out[i.body_start - 1] = 0x28
            out[i.body_end:i.body_end + 2] = b");"
    return bytes(out)


def _xml_line(line: bytes) -> bytes:
    """One line of XML text as code: escapes decoded, CDATA markers dropped, padded at the end
    of the line so ``&amp;&amp;`` reads ``&&`` and the line keeps its length."""
    decoded = _XML_TEXT.sub(lambda m: _XML_CHAR.get(m.group(0), b""), line)
    return decoded + b" " * (len(line) - len(decoded))


def el_spans(markup: bytes, *, deferred: bool) -> list[tuple[int, int]]:
    """``(start, end)`` of each EL expression in the island-free markup. ``#{ }`` counts only
    when ``deferred`` (JSP 2.1 and later, unless the page makes it a literal)."""
    out: list[tuple[int, int]] = []
    pos = 0
    while (m := _EL_OPEN.search(markup, pos)) is not None:
        start = m.start()
        if (start > 0 and markup[start - 1] == 0x5C) or (markup[start] == 0x23 and not deferred):
            pos = start + 2  # `\${` is literal text; so is `#{` before JSP 2.1
            continue
        end, stop = _el_end(markup, start + 2)
        if end is None:
            pos = stop  # unterminated: skip the scanned window, keeping the pass linear
            continue
        out.append((start, end))
        pos = end
    return out


def _el_end(src: bytes, i: int) -> tuple[int | None, int]:
    """The end of the EL expression whose body starts at ``i``, or None and where it stopped."""
    depth, quote = 1, 0
    limit = min(len(src), i + _EL_MAX)
    while i < limit:
        c = src[i]
        if quote:
            if c == 0x5C:  # backslash escape inside a string
                i += 1
            elif c == quote:
                quote = 0
        elif c in (0x22, 0x27):
            quote = c
        elif c == 0x7B:
            depth += 1
        elif c == 0x7D:
            depth -= 1
            if depth == 0:
                return i + 1, i + 1
        i += 1
    return None, max(i, limit)


class Lines:
    """Byte offset → 1-based line and 0-based byte column."""

    def __init__(self, src: bytes) -> None:
        self._starts = [0, *(m.end() for m in re.finditer(rb"\n", src))]

    def line(self, offset: int) -> int:
        return bisect_right(self._starts, offset)

    def col(self, offset: int) -> int:
        return offset - self._starts[bisect_right(self._starts, offset) - 1]
