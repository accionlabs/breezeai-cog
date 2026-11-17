"""A template page's inline ``<script>`` blocks, read as one program by every script detector.

The browser runs a page's classic inline scripts in order, in one global scope, so they are read
together: :func:`~.script_blocks.script_shadow` keeps their bodies at their true offsets and blanks
everything else. That shadow is read twice:

* by the jQuery detector, with the page's own evidence (a ``jquery*.js`` script, a Razor layout
  that loads it) — exactly as before;
* by the TypeScript parser, so every other script detector (``fetch``, ``axios``, client routers,
  …) runs over it too.

Only what a detector recognised is kept (:data:`_DETECTED`): the page's record gets its API calls,
routes and queries, not every JavaScript function and statement of its scripts. Each kept
statement hangs off the page's own records, since the script's functions are not captured.

**Server code inside a script** is not JavaScript: the caller passes its spans (``server_code``),
which are blanked before the parse. A kept statement on a line that held server code, or whose
text still shows a server expression, has its ``endpoint`` cleared — the URL was built partly on
the server and cannot be read here (honest-null) — and keeps its text as written in the page.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from ...emit import disambiguate, file_id
from ...schemas import FileRecord, Statement
from ..base import ParseContext
from ..enclosing import Enclosing
from ..treesitter import parse_source
from ..typescript.parser import TypeScriptParser
from ..typescript_jquery.calls import detect_inline_jquery
from .script_blocks import blank, script_shadow

#: The statements a script detector found that the page keeps. Not ``comment`` /
#: ``enum_member`` / other descriptive kinds: those describe the script, not what the page does.
_DETECTED = frozenset({
    "route", "api_call", "db_method_call", "query_statement",
    "eventbus_send", "eventbus_publish", "eventbus_consumer", "timer",
})

#: A server expression left in a script's text: ``<% %>`` (Web Forms, JSP), ``<?`` (PHP).
_SERVER_TAG = re.compile(r"<%|<\?")
#: Razor's ``@`` that starts an expression (``@Model.Id``, ``@(…)``, ``@{``) — not ``@@`` (an
#: escaped ``@``) and not an ``@`` glued to a word (``a@b.com``).
_RAZOR_EXPR = re.compile(r"(?<![\w.\-@])@(?=[A-Za-z_(\{])")
#: Twig's ``{{ … }}`` / ``{% … %}``.
_TWIG_EXPR = re.compile(r"\{\{|\{%")


def _server_expr(path: str) -> re.Pattern[str]:
    """The server-expression pattern for ``path``'s dialect: ``@`` is only Razor's, ``{{`` only
    Twig's (in another page it is plain JavaScript text)."""
    low = path.lower()
    if low.endswith((".cshtml", ".razor", ".vbhtml")):
        return re.compile(f"{_SERVER_TAG.pattern}|{_RAZOR_EXPR.pattern}")
    if low.endswith(".twig"):
        return re.compile(f"{_SERVER_TAG.pattern}|{_TWIG_EXPR.pattern}")
    return _SERVER_TAG


def read_page_scripts(
    record: FileRecord,
    ctx: ParseContext,
    *,
    server_code: Sequence[tuple[int, int]] = (),
    repo_root: Path | None = None,
) -> None:
    """Add to ``record`` what every script detector finds in the inline ``<script>`` blocks of
    the page ``ctx.source``. ``server_code`` is the ``(start, end)`` byte spans of the page's server
    code; ``repo_root`` (Razor) lets a layout supply the jQuery evidence. Only with statement
    capture."""
    if not ctx.capture_statements:
        return
    detect_inline_jquery(record, ctx.source, ctx.path, ctx.parse_timeout_micros, repo_root=repo_root)
    shadow, _ = script_shadow(ctx.source, ctx.path)
    if not shadow:
        return
    buf = bytearray(shadow)
    for start, end in server_code:
        blank(buf, start, end)
    shadow = bytes(buf)
    root = parse_source("typescript", shadow, ctx.parse_timeout_micros).root_node
    # The script is read on its own: the page's index is not the TypeScript one.
    script_ctx = replace(ctx, source=shadow, resolution_index=None, related_indexes={})
    found = TypeScriptParser().extract(root, script_ctx)
    _merge(record, found.statements, ctx.source, shadow, _lines_of(ctx.source, server_code),
           _server_expr(ctx.path))


def _merge(
    record: FileRecord,
    statements: list[Statement],
    source: bytes,
    shadow: bytes,
    server_lines: set[int],
    server_expr: re.Pattern[str],
) -> None:
    """Append the detected ``statements`` to ``record``, parented on the page's own records."""
    fid = file_id(record.path)
    enclosing = Enclosing(record)
    seen = {s.id for s in record.statements}
    # One call is one statement: the jQuery path already wrote its own.
    taken = {(s.startLine, s.semanticType) for s in record.statements if s.semanticType}
    for stmt in statements:
        if (stmt.semanticType not in _DETECTED or stmt.framework == "jquery"
                or (stmt.startLine, stmt.semanticType) in taken):
            continue
        stmt.parentId = enclosing.function_id(stmt.startLine, fid)
        stmt.id = disambiguate(stmt.id, seen)
        lines = range(stmt.startLine, stmt.endLine + 1)
        if any(line in server_lines for line in lines) or server_expr.search(stmt.text):
            stmt.endpoint = None
            stmt.text = _as_written(stmt.text, stmt.startLine, source, shadow)
        taken.add((stmt.startLine, stmt.semanticType))
        record.statements.append(stmt)


def _line_starts(source: bytes) -> list[int]:
    return [0] + [i + 1 for i, b in enumerate(source) if b == 0x0A]


def _lines_of(source: bytes, spans: Sequence[tuple[int, int]]) -> set[int]:
    """The 1-based lines the byte ``spans`` of ``source`` touch."""
    if not spans:
        return set()
    starts = _line_starts(source)
    lines: set[int] = set()
    for start, end in spans:
        first = bisect_right(starts, start)
        last = bisect_right(starts, max(start, end - 1))
        lines.update(range(first, last + 1))
    return lines


def _as_written(text: str, line: int, source: bytes, shadow: bytes) -> str:
    """``text`` (read from the shadow, server code blanked) as the page has it. Blanking keeps
    every byte offset, so the same bytes of ``source`` are the text as written."""
    raw = text.encode("utf-8")
    starts = _line_starts(shadow)
    if line > len(starts):
        return text
    begin = starts[line - 1]
    stop = shadow.find(b"\n", begin)
    at = shadow.find(raw, begin, len(shadow) if stop < 0 else stop)
    if at < 0:
        return text
    return source[at:at + len(raw)].decode("utf-8", "replace")
