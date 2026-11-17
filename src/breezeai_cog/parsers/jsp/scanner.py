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

Finding the constructs and splitting the page into its markup and its code is the template
splitter's job (``parsers/template_common/server_code``, JSP dialect), shared with Web Forms;
:func:`scan_islands`, :func:`shadow_markup` and :func:`shadow_java` are its JSP entry points.
This module keeps what is JSP's own: decoding, directive attributes and EL.
"""

from __future__ import annotations

import codecs
import re
from bisect import bisect_right

from ..template_common.server_code import CODE_KINDS, JSP, Island, shadow_code, shadow_markup
from ..template_common.server_code import scan_islands as _scan_islands

__all__ = ["CODE_KINDS", "Island", "Lines", "decode_source", "directive_attrs", "el_spans",
           "scan_islands", "shadow_java", "shadow_markup"]

#: One ``name="value"`` / ``name='value'`` attribute.
_ATTR = re.compile(rb"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
#: The start of an EL expression (``\\${`` is escaped and skipped by the scanner).
_EL_OPEN = re.compile(rb"[$#]\{")
#: The longest EL expression scanned; an unterminated one stops here, keeping the pass linear.
_EL_MAX = 8192
#: A declared character encoding: ``pageEncoding``, a ``contentType`` charset, the XML prolog.
_DECLARED_ENCODING = re.compile(
    rb"""(?:pageEncoding\s*=\s*["']|charset\s*=\s*|<\?xml[^>]{0,200}?encoding\s*=\s*["'])"""
    rb"""([A-Za-z0-9._:-]+)"""
)


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
    return _scan_islands(src, JSP)


def directive_attrs(src: bytes, island: Island) -> list[tuple[str, str]]:
    """The ``(name, value)`` attributes of a directive, in source order (``import`` may repeat)."""
    body = src[island.body_start:island.body_end]
    return [
        (m.group(1).decode("utf-8", "replace"),
         (m.group(2) if m.group(2) is not None else m.group(3)).decode("utf-8", "replace"))
        for m in _ATTR.finditer(body)
    ]


def shadow_java(src: bytes, islands: list[Island]) -> bytes:
    """Only the code of scriptlets, expressions and declarations, at their true offsets."""
    return shadow_code(src, islands, JSP)


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
