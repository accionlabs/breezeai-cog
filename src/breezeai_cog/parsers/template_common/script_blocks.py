"""The inline ``<script>`` blocks of a template page, at their true offsets.

:func:`script_shadow` is the page with everything except its live JavaScript blanked: the
bodies of the inline ``<script>`` blocks stay where they are, everything else becomes spaces
(newlines kept), so a node parsed from the shadow has the line, column and byte offset it has in
the page. A block that loads a file (``src=``), one whose ``type`` is not JavaScript
(``text/template``, ``application/json``…), and anything inside a comment are not code.
"""

from __future__ import annotations

import re

#: the attributes of an open tag, which may hold server code: ``<% … %>`` blocks and quoted values
#: (``src="@Url.Content("~/x.js")"`` is adjacent quoted / unquoted runs), so the ``>`` of a ``%>``
#: inside the tag does not end it early. Bounded (no real tag has 2048 attribute tokens), so a
#: pathological run of unterminated ``<script `` openers cannot make the search quadratic.
OPEN_ATTRS = rb"((?:<%.*?%>|\"[^\"]*\"|'[^']*'|[^>\"'<]|<(?!%)){0,2048})>"
SCRIPT_OPEN = re.compile(rb"<script\b" + OPEN_ATTRS, re.IGNORECASE | re.DOTALL)
_SCRIPT_CLOSE = re.compile(rb"</script\s*>", re.IGNORECASE)
SRC_ATTR = re.compile(rb"\bsrc\s*=", re.IGNORECASE)
#: a ``type=`` that is not JavaScript (``text/template``, ``application/json``…) → not a code block.
_NON_JS_TYPE = re.compile(
    rb"\btype\s*=\s*(?![\"']?(?:(?:text|application)/(?:javascript|ecmascript)|module)\b)[\"']?\w",
    re.IGNORECASE,
)
#: ``type="module"``: a block with its own scope, read as a program of its own.
_MODULE_TYPE = re.compile(rb"\btype\s*=\s*[\"']?module\b", re.IGNORECASE)
#: the ``src`` value of a ``<script>`` open tag: quoted, or a bare run up to a space.
SRC_VALUE = re.compile(rb"\bsrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'>]+))", re.IGNORECASE)
_HTML_COMMENT = (b"<!--", b"-->")
_RAZOR_COMMENT = (b"@*", b"*@")
_SERVER_TAG_COMMENT = (b"<%--", b"--%>")  # Web Forms and JSP
_TWIG_COMMENT = (b"{#", b"#}")
_NOT_NEWLINE = re.compile(rb"[^\r\n]")


def _server_comments(path: str) -> tuple[bytes, bytes] | None:
    """The server-side comment syntax of ``path``'s dialect. Such a comment is removed before the page
    is sent — wherever it sits, even inside a ``<script>`` — so it exists only for the dialect that
    owns it (``@*`` in a plain ``.html`` JavaScript string is just text). ``<!-- -->`` is different:
    an HTML comment, markup only, never inside a script body."""
    low = path.lower()
    if low.endswith((".cshtml", ".razor", ".vbhtml")):
        return _RAZOR_COMMENT
    if low.endswith((".aspx", ".ascx", ".master", ".jsp", ".jspx", ".jspf", ".tag")):
        return _SERVER_TAG_COMMENT
    if low.endswith(".twig"):
        return _TWIG_COMMENT
    return None


def blank(buf: bytearray, start: int, end: int) -> None:
    """Space out ``buf[start:end]``, keeping newlines so offsets, lines and columns stay true."""
    buf[start:end] = _NOT_NEWLINE.sub(b" ", bytes(buf[start:end]))


def sweep(markup: bytes, path: str) -> tuple[bytearray, list[tuple[int, int, bytes]]]:
    """One left-to-right pass over ``markup``: a copy with every comment blanked, plus the
    ``(body_start, body_end, attributes)`` of each ``<script>`` that is live (not inside a comment).
    Walking in order is what lets a comment hide a ``<script>`` while a ``<script>`` body hides an
    ``<!--`` that is merely JavaScript text."""
    server = _server_comments(path)
    closers = dict([_HTML_COMMENT] + ([server] if server else []))
    opener = re.compile(b"|".join([rb"<!--", rb"<script\b"] + [re.escape(o) for o in closers if o != b"<!--"]),
                        re.IGNORECASE)
    clean = bytearray(markup)
    scripts: list[tuple[int, int, bytes]] = []
    pos = 0
    while (m := opener.search(markup, pos)) is not None:
        tok = m.group(0)
        if not tok.lower().startswith(b"<script"):  # a comment: blank it up to its close (or EOF)
            end = markup.find(closers[tok], m.end())
            end = len(markup) if end < 0 else end + len(closers[tok])
            blank(clean, m.start(), end)
            pos = end
            continue
        tag = SCRIPT_OPEN.match(markup, m.start())
        close = _SCRIPT_CLOSE.search(markup, tag.end()) if tag is not None else None
        if tag is None or close is None:
            pos = m.end()
            continue
        if server:  # a server comment inside the body vanishes before the browser sees it
            rx = re.compile(re.escape(server[0]) + rb".*?" + re.escape(server[1]), re.DOTALL)
            for c in rx.finditer(markup, tag.end(), close.start()):
                blank(clean, c.start(), c.end())
        scripts.append((tag.end(), close.start(), tag.group(1)))
        pos = close.end()
    return clean, scripts


def script_shadow(markup: bytes, path: str = "") -> tuple[bytes, bytes]:
    """``(shadow, clean)``: ``markup`` with everything except its live inline ``<script>`` bodies
    blanked (``b""`` when it has none), and ``markup`` with only its comments blanked."""
    clean, scripts = sweep(markup, path)
    keep = bytearray(_NOT_NEWLINE.sub(b" ", markup))
    found = False
    for body_start, body_end, attrs in scripts:
        if SRC_ATTR.search(attrs) or _NON_JS_TYPE.search(attrs):
            continue
        keep[body_start:body_end] = clean[body_start:body_end]
        found = True
    return (bytes(keep) if found else b""), bytes(clean)


def script_programs(markup: bytes, path: str = "") -> list[bytes]:
    """The page's inline scripts as the browser runs them, each a shadow at true offsets: the
    classic blocks joined as one program (they share one global scope), then one program per
    ``type="module"`` block (each has its own scope). Empty programs are left out."""
    clean, scripts = sweep(markup, path)
    blanked = _NOT_NEWLINE.sub(b" ", markup)
    classic = bytearray(blanked)
    programs: list[bytes] = []
    has_classic = False
    for body_start, body_end, attrs in scripts:
        if SRC_ATTR.search(attrs) or _NON_JS_TYPE.search(attrs):
            continue
        if _MODULE_TYPE.search(attrs):
            module = bytearray(blanked)
            module[body_start:body_end] = clean[body_start:body_end]
            programs.append(bytes(module))
            continue
        classic[body_start:body_end] = clean[body_start:body_end]
        has_classic = True
    return ([bytes(classic)] if has_classic else []) + programs


def script_sources(markup: bytes, path: str = "") -> list[str]:
    """The ``src`` of every live ``<script src=…>`` tag of the page, as written, in page order."""
    _, scripts = sweep(markup, path)
    found: list[str] = []
    for _, _, attrs in scripts:
        m = SRC_VALUE.search(attrs)
        if m is not None:
            found.append(next(g for g in m.groups() if g is not None).decode("utf-8", "replace"))
    return found
