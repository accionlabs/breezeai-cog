"""Hand-rolled line-scan structural extractor for JSLT.

JSLT (github.com/schibsted/jslt) has no tree-sitter grammar in this project's language
pack, and the only public third-party grammar is a single-author repository last touched in
2023 with no PyPI package or verified Python bindings — it fails this project's Vet Before
Reuse bar (see the plan's Decisions table). Instead of adopting it, this module does a
pragmatic line-scan over JSLT's small, line-oriented top-level surface: ``import``, ``def``,
and ``let`` declarations are conventionally unindented (column 0), and JSLT's own grammar
requires every declaration to precede the module's single trailing output expression (never
interleaved) — so the **first** column-0 line that isn't a declaration keyword, *and* isn't
inside an unclosed ``{``/``[``/``(`` from a still-open multi-line value, marks "declarations
are over"; everything from there to EOF is captured as one ``module_expression``, without
trying to sub-parse it further.

The bracket-depth tracking matters because closing a multi-line JSON-style value flush at
column 0 (e.g. a ``let x = {\\n  ...\\n}``) is ordinary JSLT formatting, not unconventional
style — without it, that closing ``}`` would be misread as a new top-level construct and
truncate the declaration's captured text.

This is still an honest structural approximation, not a real JSLT parser: nested ``let``s
inside a ``def`` body (indented) are correctly left inside that ``def``'s captured text,
never split out — same "full body on text" philosophy as ``prisma/schema.py``.

No tree-sitter grammar also means no shared ``comments_common.py`` pass is available; ``//``
comments are not captured as their own statements in this v1 (out of scope — see the plan).
"""

from __future__ import annotations

import re

from ...emit import disambiguate, file_id, statement_id
from ...schemas import Statement

_DECL_RE = re.compile(r"^(import|def|let)\b")
_IMPORT_ALIAS_RE = re.compile(r'^import\s+"[^"]*"\s+as\s+(\S+)')
_DEF_NAME_RE = re.compile(r"^def\s+([A-Za-z_][\w-]*)\s*\(")
_LET_NAME_RE = re.compile(r"^let\s+([A-Za-z_][\w-]*)\s*=")
_NAME_PATTERNS = {"import": _IMPORT_ALIAS_RE, "def": _DEF_NAME_RE, "let": _LET_NAME_RE}

_NODE_TYPES = {"import": "import", "def": "function_def", "let": "let_binding"}
_OPEN_BRACKETS = "{[("
_CLOSE_BRACKETS = "}])"


def _strip_line_comment(line: str) -> str:
    """Cut ``line`` at the first ``//`` that isn't inside a double-quoted string (JSLT
    strings use ``\\"`` escapes)."""
    in_string = False
    i = 0
    while i < len(line) - 1:
        ch = line[i]
        if ch == "\\" and in_string:
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string and ch == "/" and line[i + 1] == "/":
            return line[:i]
        i += 1
    return line


def _is_blank_or_comment(line: str) -> bool:
    return _strip_line_comment(line).strip() == ""


def _bracket_delta(line: str) -> int:
    """Net change in ``{}``/``[]``/``()`` nesting depth caused by ``line`` (comment- and
    string-aware, like ``_strip_line_comment``)."""
    stripped = _strip_line_comment(line)
    delta = 0
    in_string = False
    i = 0
    while i < len(stripped):
        ch = stripped[i]
        if ch == "\\" and in_string:
            i += 2
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string and ch in _OPEN_BRACKETS:
            delta += 1
        elif not in_string and ch in _CLOSE_BRACKETS:
            delta -= 1
        i += 1
    return delta


def _declared_name(keyword: str, first_line: str) -> str | None:
    m = _NAME_PATTERNS[keyword].match(first_line)
    return m.group(1) if m else None


def _boundaries(lines: list[str]) -> list[int]:
    """Column-0, non-blank/non-comment line numbers (1-based) that sit at bracket-nesting
    depth 0 — genuine new top-level constructs, not a continuation/closing line of a
    still-open multi-line value."""
    out: list[int] = []
    depth = 0
    for i, line in enumerate(lines):
        if depth == 0 and line[:1] not in ("", " ", "\t") and not _is_blank_or_comment(line):
            out.append(i + 1)
        depth += _bracket_delta(line)
    return out


def _split_declarations(
    lines: list[str], boundaries: list[int]
) -> tuple[list[tuple[int, str]], int | None]:
    """Split ``boundaries`` into leading ``(line, keyword)`` declaration starts and the line
    where the trailing module expression begins — JSLT requires every declaration to precede
    the module's single final expression, never interleaved, so the first non-declaration
    boundary ends the declaration section."""
    decl_starts: list[tuple[int, str]] = []
    for ln in boundaries:
        m = _DECL_RE.match(_strip_line_comment(lines[ln - 1]))
        if not m:
            return decl_starts, ln
        decl_starts.append((ln, m.group(1)))
    return decl_starts, None


def _trim_trailing_blank(lines: list[str], start: int, end: int) -> int:
    """``end``, walked back past any trailing blank/comment-only lines (never below
    ``start``, whose own line is always real content by construction)."""
    while end > start and _is_blank_or_comment(lines[end - 1]):
        end -= 1
    return end


def _build_statement(
    fid: str, path: str, lines: list[str], seen_ids: set[str],
    node_type: str, name: str | None, start: int, end: int,
) -> Statement:
    text = "\n".join(lines[start - 1 : end])
    return Statement(
        id=disambiguate(statement_id(path, start, 0), seen_ids),
        parentId=fid,
        path=path,
        nodeType=node_type,
        name=name,
        text=text,
        startLine=start,
        endLine=end,
    )


def collect_jslt_statements(source: str, path: str, seen_ids: set[str]) -> list[Statement]:
    """Line-scan a JSLT document and emit one flat ``Statement`` per top-level (column-0,
    depth-0) ``import``/``def``/``let`` declaration, plus one trailing ``module_expression``
    for the output expression after the last declaration (empty file → no statements)."""
    fid = file_id(path)
    lines = source.splitlines()
    n = len(lines)
    out: list[Statement] = []

    def build(node_type: str, name: str | None, start: int, end: int) -> Statement:
        return _build_statement(fid, path, lines, seen_ids, node_type, name, start, end)

    decl_starts, module_start = _split_declarations(lines, _boundaries(lines))
    decl_end_limit = (module_start - 1) if module_start is not None else n

    for idx, (start, keyword) in enumerate(decl_starts):
        raw_end = decl_starts[idx + 1][0] - 1 if idx + 1 < len(decl_starts) else decl_end_limit
        end = _trim_trailing_blank(lines, start, raw_end)
        first = _strip_line_comment(lines[start - 1])
        out.append(build(_NODE_TYPES[keyword], _declared_name(keyword, first), start, end))

    if module_start is not None:
        hi = _trim_trailing_blank(lines, module_start, n)
        out.append(build("module_expression", None, module_start, hi))

    return out
