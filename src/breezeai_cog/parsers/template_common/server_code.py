"""The splitter: a template page's server code, found in one linear pass and split from its markup.

JSP and ASP.NET Web Forms share the ``<% %>`` syntax, so one scanner reads both; a
:class:`Dialect` holds what differs:

* **JSP** (every version, 1.0 through 4.0): ``<%-- --%>`` comments, ``<%@ %>`` directives,
  ``<%= %>`` expressions, ``<%! %>`` declarations, ``<% %>`` scriptlets, and the XML syntax
  (``<jsp:scriptlet>``, ``<jsp:expression>``, ``<jsp:declaration>``, ``<jsp:directive.*>``) whose
  bodies are XML text (``&lt;``, ``<![CDATA[ ]]>``), decoded before the code is parsed (per line,
  so lines stay true; columns after an escape on its line move left).
* **ASP.NET** (Web Forms): the same comments, directives, ``<%= %>`` and ``<% %>``, plus ``<%: %>``
  (encoded output), ``<%# %>`` / ``<%#: %>`` (data binding), ``<%$ %>`` (an expression builder:
  ``Resources:…``, ``ConnectionStrings:…`` — not code) and ``<script runat="server">`` (members
  of the page class).

The spec's own escapes keep the scan exact: template text writes a literal ``<%`` as ``<\\%`` and
code writes a literal ``%>`` as ``%\\>``, so a plain search for the delimiters never splits an
element.

The two **shadows** are copies of the page with the same length (newlines kept), so every offset,
line and column taken from a tree parsed over a shadow is true for the page:

* :func:`shadow_markup` blanks every island, leaving the markup for the ``html`` grammar;
* :func:`shadow_code` keeps only the code of the code islands, for the page's language grammar
  (``java``, ``c_sharp``), which accepts statements and method declarations at the top level. An
  expression is wrapped so it parses as a statement: ``<%= x %>`` becomes ``  (x);`` in Java and
  ``_= x ;`` in C#.

PHP pages (``.phtml``) and Twig have a grammar that already splits the page: its text parts are
the markup, so :func:`code_outside` gives everything else as the server code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tree_sitter import Node

from ...schemas import Statement
from ...utils.text import truncate

#: ``bytes.translate`` table that blanks every byte except a newline.
_BLANK = bytes(0x0A if i == 0x0A else 0x20 for i in range(256))

#: An XML-syntax directive's start tag (attributes only, so ``>`` inside a value is safe).
_XML_DIRECTIVE = re.compile(
    rb"""<jsp:directive\.([A-Za-z]+)((?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(/?)>"""
)
#: A directive name at the start of a ``<%@ … %>`` body.
_DIRECTIVE_NAME = re.compile(rb"\s*([A-Za-z]+)")
#: XML text escapes and CDATA markers inside an XML-syntax scripting element.
_XML_TEXT = re.compile(rb"&(?:lt|gt|amp|quot|apos);|<!\[CDATA\[|\]\]>")
_XML_CHAR = {b"&lt;": b"<", b"&gt;": b">", b"&amp;": b"&", b"&quot;": b'"', b"&apos;": b"'"}
#: A ``<script>`` start tag (attributes only, so ``>`` inside a value is safe).
_SCRIPT_TAG = re.compile(rb"""<script\b((?:\s+[\w:.-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*>""",
                         re.IGNORECASE)
_RUNAT_SERVER = re.compile(rb"""\brunat\s*=\s*["']?server\b""", re.IGNORECASE)
_SCRIPT_CLOSE = re.compile(rb"</script\s*>", re.IGNORECASE)

#: Kinds whose body is code in the page's language.
CODE_KINDS = frozenset({"scriptlet", "expression", "declaration", "binding"})
#: Code kinds whose body is one expression, wrapped so it parses as a statement.
_EXPRESSION_KINDS = frozenset({"expression", "binding"})


@dataclass(frozen=True, slots=True)
class Dialect:
    """What one server-page technology adds to the shared ``<% %>`` syntax."""

    name: str
    #: The byte after ``<%`` → the island kind; any other byte opens a scriptlet.
    markers: dict[bytes, str] = field(default_factory=dict)
    #: JSP's XML syntax (``<jsp:scriptlet>`` …).
    xml_forms: bool = False
    #: ASP.NET's ``<script runat="server">`` blocks (members of the page class).
    server_scripts: bool = False
    #: How an expression becomes a statement: text written just before its body (ending where
    #: the body starts) and just after it (where the closer was).
    wrap: tuple[bytes, bytes] = (b"(", b");")


JSP = Dialect(
    "jsp",
    markers={b"@": "directive", b"=": "expression", b"!": "declaration"},
    xml_forms=True,
)
ASPNET = Dialect(
    "aspnet",
    markers={b"@": "directive", b"=": "expression", b":": "expression", b"#": "binding",
             b"$": "expression_builder"},
    server_scripts=True,
    wrap=(b"_=", b";"),
)


@dataclass(frozen=True, slots=True)
class Island:
    """One server construct: scripting element, directive, comment or server script.

    ``start``/``end`` cover the whole construct; ``body_start``/``body_end`` its content (the
    code, the directive's name and attributes, or the comment text). ``closed`` is False when
    the file ends before the closing delimiter; ``xml`` marks JSP's XML-syntax form."""

    #: comment | directive | expression | declaration | scriptlet | binding | expression_builder
    kind: str
    start: int
    end: int
    body_start: int
    body_end: int
    closed: bool
    xml: bool = False
    name: str | None = None  # the directive name (page, include, taglib, tag, Control, …)


def _opener(dialect: Dialect) -> re.Pattern[bytes]:
    parts = [rb"<%"]
    if dialect.xml_forms:
        parts.append(rb"<jsp:(scriptlet|expression|declaration|directive\.[A-Za-z]+)(?=[\s/>])")
    if dialect.server_scripts:
        parts.append(rb"(?i:<script\b)")
    return re.compile(b"|".join(parts))


_OPENERS: dict[str, re.Pattern[bytes]] = {}


def scan_islands(src: bytes, dialect: Dialect) -> list[Island]:
    """Every server construct of ``src`` in ``dialect``, in source order (one pass)."""
    opener = _OPENERS.get(dialect.name)
    if opener is None:
        opener = _OPENERS[dialect.name] = _opener(dialect)
    out: list[Island] = []
    pos, n = 0, len(src)
    while (m := opener.search(src, pos)) is not None:
        start = m.start()
        if src.startswith(b"<%", start):
            island: Island | None = _standard(src, start, n, dialect)
        elif src.startswith(b"<jsp:", start):
            island = _xml(src, m, n)
        else:
            island = _server_script(src, start, n)
        if island is None:
            pos = m.end()
            continue
        out.append(island)
        pos = max(island.end, start + 2)
    return out


def _standard(src: bytes, start: int, n: int, dialect: Dialect) -> Island:
    if src.startswith(b"<%--", start):
        kind, body, close = "comment", start + 4, b"--%>"
    else:
        marker = src[start + 2:start + 3]
        kind = dialect.markers.get(marker, "scriptlet")
        body, close = (start + 2 if kind == "scriptlet" else start + 3), b"%>"
        if kind == "binding" and src.startswith(b":", body):  # <%#: x %>, the encoded binding
            body += 1
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


def _server_script(src: bytes, start: int, n: int) -> Island | None:
    """``<script runat="server">…</script>``: the page class's members. A browser script is not
    an island (its ``<% %>`` islands are found on their own)."""
    tag = _SCRIPT_TAG.match(src, start)
    if tag is None or not _RUNAT_SERVER.search(tag.group(1)):
        return None
    close = _SCRIPT_CLOSE.search(src, tag.end())
    closed = close is not None
    body_end = close.start() if close is not None else n
    return Island("declaration", start, close.end() if close is not None else n, tag.end(),
                  body_end, closed)


def shadow_markup(src: bytes, islands: list[Island]) -> bytes:
    """``src`` with every island blanked: the markup the ``html`` grammar parses."""
    out = bytearray(src)
    for i in islands:
        out[i.start:i.end] = src[i.start:i.end].translate(_BLANK)
    return bytes(out)


def shadow_code(src: bytes, islands: list[Island], dialect: Dialect) -> bytes:
    """Only the code of the code islands, at their true offsets."""
    out = bytearray(src.translate(_BLANK))
    before, after = dialect.wrap
    for i in islands:
        if i.kind not in CODE_KINDS:
            continue
        body = src[i.body_start:i.body_end]
        if i.xml:
            body = b"\n".join(_xml_line(line) for line in body.split(b"\n"))
        out[i.body_start:i.body_end] = body
        if i.kind in _EXPRESSION_KINDS and i.closed and i.body_start - i.start >= len(before):
            # the opener's last bytes and the closer's first ones: `<%= x %>` → `  (x);`
            out[i.body_start - len(before):i.body_start] = before
            out[i.body_end:i.body_end + len(after)] = after
    return bytes(out)


def _xml_line(line: bytes) -> bytes:
    """One line of XML text as code: escapes decoded, CDATA markers dropped, padded at the end
    of the line so ``&amp;&amp;`` reads ``&&`` and the line keeps its length."""
    decoded = _XML_TEXT.sub(lambda m: _XML_CHAR.get(m.group(0), b""), line)
    return decoded + b" " * (len(line) - len(decoded))


def code_outside(root: Node, text_type: str) -> list[tuple[int, int]]:
    """The ``(start, end)`` spans of ``root``'s page that are not a ``text_type`` node (PHP's
    ``text``, Twig's ``content``): the server code, with the whitespace between its parts."""
    texts: list[tuple[int, int]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == text_type:
            texts.append((node.start_byte, node.end_byte))
        else:
            stack.extend(node.children)
    spans: list[tuple[int, int]] = []
    at = root.start_byte
    for start, end in sorted(texts):
        if start > at:
            spans.append((at, start))
        at = max(at, end)
    if at < root.end_byte:
        spans.append((at, root.end_byte))
    return spans


def expressions_as_written(
    statements: list[Statement], source: bytes, islands: list[Island], dialect: Dialect,
    limit: int,
) -> None:
    """An expression's statement cites the page (``<%= x %>``), not the wrapper the grammar
    parsed (``(x);`` in Java, ``_= x ;`` in C#)."""
    opener, closer = (part.decode() for part in dialect.wrap)
    by_line: dict[tuple[int, str], Island] = {}
    for i in islands:
        if i.kind in _EXPRESSION_KINDS:
            body = source[i.body_start:i.body_end].decode("utf-8", "replace").strip()
            by_line[(source.count(b"\n", 0, i.start) + 1, body)] = i
    for stmt in statements:
        text = stmt.text.strip()
        if not (text.startswith(opener) and text.endswith(closer)):
            continue
        body = text[len(opener):len(text) - len(closer)].strip()
        island = by_line.get((stmt.startLine, body))
        if island is not None:
            stmt.text = truncate(source[island.start:island.end].decode("utf-8", "replace"), limit)
