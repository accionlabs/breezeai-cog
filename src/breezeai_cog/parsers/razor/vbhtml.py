"""VB Razor views (``.vbhtml``, ASP.NET MVC) — the ``.cshtml`` template statements, read by a text
scan.

The ``razor`` grammar is C# Razor: on a VB view ``@ModelType``, ``@Code … End Code``,
``@For Each … Next`` and ``@If … End If`` become ``ERROR`` nodes that swallow the markup around
them, and there is no VB Razor grammar. So a ``.vbhtml`` file is scanned as text, and every record
is ``synthetic`` (no tree node backs it)::

    @ModelType IEnumerable(Of Shop.Order)     → model statement, name "IEnumerable(Of Shop.Order)"
    @Html.ActionLink("New", "Create")         → expression, name 'Html.ActionLink("New", "Create")'
    @For Each item In Model                   → control flow (semanticType null), the header line
    @item.Total                               → expression, name "item.Total"

An ``@`` is code only where VB Razor reads it as code: not after a letter or digit (an e-mail
address), not doubled (``@@``), and outside ``@* … *@`` comments, ``<script>`` / ``<style>`` bodies
and ``@Code`` / ``@Functions`` / ``@Helper`` blocks (VB code, not template). An expression is a
name followed by ``.name`` and balanced ``( … )`` on its line; one whose parentheses do not close
on the line is left out. VB Razor has no ``@page``: a view is reached through its controller.
"""

from __future__ import annotations

import re

from ...emit import disambiguate, statement_id
from ...schemas import Statement

_NOT_NEWLINE = re.compile(rb"[^\r\n]")
#: Regions whose ``@`` is not template code: comments, script/style bodies, VB code blocks.
_SKIPPED = re.compile(
    rb"@\*.*?\*@"
    rb"|<script\b[^>]*>.*?</script\s*>"
    rb"|<style\b[^>]*>.*?</style\s*>"
    rb"|@(?:Code|Functions|Helper)\b.*?^[ \t]*End[ \t]+(?:Code|Functions|Helper)\b",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)
#: An ``@`` that starts code: not after a word character or another ``@``, not before ``@``.
_AT = re.compile(rb"(?<![\w@])@(?!@)")
_MODEL = re.compile(rb"ModelType[ \t]+([^\r\n']+?)[ \t]*(?:'[^\r\n]*)?\r?$", re.IGNORECASE | re.MULTILINE)
_CONTROL = re.compile(
    rb"(?:For[ \t]+Each|For|If|Select[ \t]+Case|While|Do|Using|With|Try|SyncLock)\b", re.IGNORECASE)
#: Directives and blocks that are not expressions.
_NOT_EXPRESSION = frozenset({
    "modeltype", "code", "functions", "helper", "section", "imports", "inherits", "end", "next",
    "loop", "else", "elseif", "case", "catch", "finally",
})
_NAME = re.compile(rb"[A-Za-z_]\w*")


def _expression_end(source: bytes, start: int) -> int | None:
    """The end of an implicit expression starting at ``start`` (a name): further ``.name`` and
    balanced ``( … )`` on the same line. None when a parenthesis does not close on the line."""
    m = _NAME.match(source, start)
    if m is None:
        return None
    pos = m.end()
    while pos < len(source):
        ch = source[pos:pos + 1]
        if ch == b"." and (nxt := _NAME.match(source, pos + 1)) is not None:
            pos = nxt.end()
        elif ch == b"(":
            depth, i, in_str = 0, pos, False
            while i < len(source) and source[i:i + 1] not in (b"\n", b"\r"):
                c = source[i:i + 1]
                if c == b'"':
                    in_str = not in_str  # VB's "" escape toggles twice: still balanced
                elif not in_str and c == b"(":
                    depth += 1
                elif not in_str and c == b")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if depth != 0 or i >= len(source) or source[i:i + 1] != b")":
                return None
            pos = i + 1
        else:
            break
    return pos


def collect_vbhtml_statements(
    source: bytes, path: str, parent_id: str, seen_ids: set[str], limit: int,
) -> list[Statement]:
    """The template statements of a ``.vbhtml`` view (model, expressions, control flow)."""
    clean = bytearray(source)
    for m in _SKIPPED.finditer(source):
        clean[m.start():m.end()] = _NOT_NEWLINE.sub(b" ", source[m.start():m.end()])
    scan = bytes(clean)
    out: list[Statement] = []

    def emit(start: int, end: int, **fields: object) -> None:
        line = source.count(b"\n", 0, start) + 1
        col = start - (source.rfind(b"\n", 0, start) + 1)
        text = source[start:end].decode("utf-8", "replace").rstrip()
        out.append(Statement(
            id=disambiguate(statement_id(path, line, col), seen_ids), parentId=parent_id,
            nodeType="synthetic", text=text[:limit], startLine=line, endLine=line, path=path,
            **fields,  # type: ignore[arg-type]
        ))

    for at in _AT.finditer(scan):
        start, body = at.start(), at.end()
        if (model := _MODEL.match(scan, body)) is not None:
            emit(start, model.end(1), name=model.group(1).decode("utf-8", "replace"))
        elif _CONTROL.match(scan, body) is not None:
            eol = scan.find(b"\n", body)
            emit(start, len(scan) if eol == -1 else eol)
        elif (name := _NAME.match(scan, body)) is not None \
                and name.group(0).decode().lower() not in _NOT_EXPRESSION:
            end = _expression_end(scan, body)
            if end is not None:
                emit(start, end, name=source[body:end].decode("utf-8", "replace"))
    return out
