"""Hand-rolled line-scan structural extractor for JSLT.

JSLT (github.com/schibsted/jslt) has no tree-sitter grammar in this project's language
pack, and the only public third-party grammar is a single-author repository last touched in
2023 with no PyPI package or verified Python bindings — it fails this project's Vet Before
Reuse bar (see the plan's Decisions table). Instead of adopting it, this module does a
pragmatic line-scan over JSLT's small, line-oriented top-level surface: ``import``, ``def``,
and ``let`` declarations are conventionally unindented (column 0), and JSLT's own grammar
requires every declaration to precede the module's single trailing output expression (never
interleaved) — so the **first** column-0 line that isn't a declaration keyword marks
"declarations are over"; everything from there to EOF is captured as one
``module_expression``, without trying to sub-parse it further.

This is an honest structural approximation, not a real JSLT parser: a continuation line of a
multi-line ``let``/``def`` body that itself happens to start at column 0 (unconventional
style) would be mis-read as a new top-level declaration. Nested ``let``s inside a ``def``
body (indented) are correctly left inside that ``def``'s captured text, never split out —
same "full body on text" philosophy as ``prisma/schema.py``.

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

_NODE_TYPES = {"import": "import", "def": "function_def", "let": "let_binding"}


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


def _declared_name(keyword: str, first_line: str) -> str | None:
    pattern = {"import": _IMPORT_ALIAS_RE, "def": _DEF_NAME_RE, "let": _LET_NAME_RE}[keyword]
    m = pattern.match(first_line)
    return m.group(1) if m else None


def collect_jslt_statements(
    source: str, path: str, seen_ids: set[str], limit: int
) -> list[Statement]:
    """Line-scan a JSLT document and emit one flat ``Statement`` per top-level (column-0)
    ``import``/``def``/``let`` declaration, plus one trailing ``module_expression`` for the
    output expression after the last declaration (empty file → no statements)."""
    fid = file_id(path)
    lines = source.splitlines()
    n = len(lines)
    out: list[Statement] = []

    # Column-0, non-blank/non-comment line numbers (1-based), in source order.
    boundaries = [
        i + 1
        for i, line in enumerate(lines)
        if line[:1] not in ("", " ", "\t") and not _is_blank_or_comment(line)
    ]

    decl_starts: list[tuple[int, str]] = []
    module_start: int | None = None
    for ln in boundaries:
        m = _DECL_RE.match(_strip_line_comment(lines[ln - 1]))
        if m:
            decl_starts.append((ln, m.group(1)))
        else:
            module_start = ln
            break

    def emit(node_type: str, name: str | None, start: int, end: int) -> None:
        text = "\n".join(lines[start - 1 : end])[:limit]
        out.append(
            Statement(
                id=disambiguate(statement_id(path, start, 0), seen_ids),
                parentId=fid,
                path=path,
                nodeType=node_type,
                name=name,
                text=text,
                startLine=start,
                endLine=end,
            )
        )

    decl_end_limit = (module_start - 1) if module_start is not None else n
    for idx, (start, keyword) in enumerate(decl_starts):
        raw_end = decl_starts[idx + 1][0] - 1 if idx + 1 < len(decl_starts) else decl_end_limit
        end = raw_end
        while end > start and _is_blank_or_comment(lines[end - 1]):
            end -= 1
        first = _strip_line_comment(lines[start - 1])
        emit(_NODE_TYPES[keyword], _declared_name(keyword, first), start, end)

    if module_start is not None:
        hi = n
        while hi >= module_start and _is_blank_or_comment(lines[hi - 1]):
            hi -= 1
        if hi >= module_start:
            emit("module_expression", None, module_start, hi)

    return out
