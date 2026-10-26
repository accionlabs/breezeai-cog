"""Hand-rolled line-scan structural extractor for JSLT.

JSLT (github.com/schibsted/jslt) has no tree-sitter grammar in this project's language
pack, and the only public third-party grammar is a single-author repository last touched in
2023 with no PyPI package or verified Python bindings — not safe to build on. Instead, this
module does a pragmatic line-scan over JSLT's small, line-oriented top-level surface:
``import``, ``def``, and ``let`` declarations are conventionally unindented (column 0), and
JSLT's own grammar requires every declaration to precede the module's single trailing output
expression (never interleaved) — so the **first** column-0 line that isn't a declaration
keyword, *and* isn't inside an unclosed ``{``/``[``/``(`` from a still-open multi-line value,
*and* isn't the value/body of a still-pending header (see below), marks "declarations are
over"; everything from there to EOF is captured as one ``module_expression``, without trying
to sub-parse it further.

Two things keep a declaration's value/body from being cut short:

* **Bracket-depth tracking** — closing a multi-line JSON-style value flush at column 0 (e.g.
  a ``let x = {\\n  ...\\n}``) is ordinary JSLT formatting, not unconventional style; without
  tracking nesting depth, that closing ``}`` would be misread as a new top-level construct.
* **Incomplete-header tracking** (``_INCOMPLETE_HEADER_RE``) — a header can also end with
  *nothing* after the ``=``/``)``, so the value/body only starts on the next line (``let x
  =\\n{...}`` or ``def f(a)\\nbody``); the next column-0 line is still part of that
  declaration, not a new boundary.

``def`` declarations are emitted as ``Function`` records (not statements) — so a JSLT
function is independently searchable and call-linked, the same as any other language's
functions. Parameters carry no type (JSLT is untyped), so ``Parameter.type=""``, same
convention ``python/functions.py`` uses for an untyped parameter. ``calls[]`` resolves only
same-file calls (a bare ``name(…)`` matching another ``def`` in this file, via the shared
``callresolve.make_resolver`` — Tier 2, same as every other language) — honest-null for
everything else (built-ins, calls inside the trailing ``module_expression``, which isn't a
``Function`` and so has no ``calls`` field to carry a resolved edge on). ``Function`` has no
``text`` field, so the full body is carried on a companion ``function_body`` statement
parented to it (``parentId`` = the Function's id, not the file's) — same "full body on text"
philosophy as ``prisma/schema.py``, just one hop further down.

This is still an honest structural approximation, not a real JSLT parser: nested ``let``s
inside a ``def`` body (indented) are correctly left inside that body's captured text, never
split out.

No tree-sitter grammar also means no shared ``comments_common.py`` pass is available; ``//``
comments are not captured as their own statements.
"""

from __future__ import annotations

import re

from ...emit import disambiguate, file_id, function_id, statement_id
from ...schemas import Call, Function, Parameter, Statement
from ..callresolve import CallResolver, make_resolver

_DECL_RE = re.compile(r"^(import|def|let)\b")
_IMPORT_ALIAS_RE = re.compile(r'^import\s+"[^"]*"\s+as\s+(\S+)')
_DEF_HEADER_RE = re.compile(r"^def\s+([A-Za-z_][\w-]*)\s*\(([^)]*)\)")
_LET_NAME_RE = re.compile(r"^let\s+([A-Za-z_][\w-]*)\s*=")
#: A bare `name(` call. The lookbehind rejects a name that is only the tail of something
#: else: `m:foo(` calls `foo` in the module imported `as m` (not this file's `foo`), and a
#: preceding word character, `-`, `$` or `.` means the match started mid-identifier.
_CALL_RE = re.compile(r"(?<![\w:$.-])([A-Za-z_][\w-]*)\s*\(")

_NODE_TYPES = {"import": "import", "let": "let_binding"}
_OPEN_BRACKETS = "{[("
_CLOSE_BRACKETS = "}])"

#: A `let`/`def` header whose value/body hasn't started yet on this line — `let x =` with
#: nothing after the `=`, or `def f(a, b)` with nothing after the closing `)`. The next
#: column-0 line is still part of *this* declaration, not a new boundary.
_INCOMPLETE_HEADER_RE = re.compile(r"^(let\s+[\w-]+\s*=|def\s+[\w-]+\s*\([^)]*\))\s*$")


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


def _code_only(line: str) -> str:
    """``line`` with its ``//`` comment and every double-quoted string literal (quotes
    included) replaced by spaces — only code is left, at the same columns, so an offset into
    ``line`` is still valid in the result. An unterminated string blanks the rest of the
    line."""
    stripped = _strip_line_comment(line)
    out = list(stripped)
    in_string = False
    i = 0
    while i < len(stripped):
        ch = stripped[i]
        if in_string:
            out[i] = " "
            if ch == "\\" and i + 1 < len(stripped):
                out[i + 1] = " "
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out[i] = " "
        i += 1
    return "".join(out) + " " * (len(line) - len(stripped))


def _bracket_delta(line: str) -> int:
    """Net change in ``{}``/``[]``/``()`` nesting depth caused by ``line``, ignoring
    brackets inside comments and strings."""
    code = _code_only(line)
    return sum(code.count(c) for c in _OPEN_BRACKETS) - sum(code.count(c) for c in _CLOSE_BRACKETS)


def _boundaries(lines: list[str]) -> list[int]:
    """Column-0, non-blank/non-comment line numbers (1-based) that sit at bracket-nesting
    depth 0 — genuine new top-level constructs, not a continuation/closing line of a
    still-open multi-line value, and not the value/body of a `let`/`def` whose header ended
    with nothing after the `=`/`)` (so the value/body only starts on the next line)."""
    out: list[int] = []
    depth = 0
    continues = False
    for i, line in enumerate(lines):
        code = _strip_line_comment(line)
        if not code.strip():
            pass  # blank/comment-only: a pending header still continues
        elif depth == 0 and line[:1] not in (" ", "\t"):
            if not continues:
                out.append(i + 1)
            continues = bool(_INCOMPLETE_HEADER_RE.match(code.rstrip()))
        else:
            continues = False  # an indented (or depth>0) line is the pending value/body itself
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


def _def_params(raw: str) -> list[Parameter]:
    """Parse a `def` header's raw parameter-list text. JSLT parameters carry no type
    annotation, so `type=""` — same convention `python/functions.py` uses for an untyped
    parameter (a required schema field with nothing real to put in it)."""
    return [Parameter(name=p.strip(), type="") for p in raw.split(",") if p.strip()]


def _same_file_calls(lines: list[str], header_end: int, resolve: CallResolver) -> list[Call]:
    """Bare `name(...)` calls in a `def`'s `lines` that resolve to another `def` in this same
    file — scanned from `header_end` onward (the header's own `def name(` is excluded, or
    `name` would read as a spurious call to itself). Comments and string literals are blanked
    first, so a name written in either is never read as a call."""
    code = "\n".join(_code_only(line) for line in lines)
    seen: dict[str, Call] = {}
    for m in _CALL_RE.finditer(code[header_end:]):
        name = m.group(1)
        if name in seen:
            continue
        resolved_path = resolve(name, None)
        if resolved_path is not None:
            seen[name] = Call(name=name, path=resolved_path)
    return list(seen.values())


def collect_jslt_declarations(
    source: str, path: str, seen_ids: set[str], *, capture_statements: bool
) -> tuple[list[Statement], list[Function]]:
    """Line-scan a JSLT document and emit: one ``Function`` per top-level (column-0,
    depth-0) ``def``, one ``Statement`` per ``import``/``let`` declaration, plus one trailing
    ``module_expression`` statement for the output expression after the last declaration
    (empty file → nothing). ``Function.calls`` resolves same-file calls only. Functions are
    structure and always emitted; statements only when ``capture_statements`` is set."""
    fid = file_id(path)
    lines = source.splitlines()
    n = len(lines)
    statements: list[Statement] = []

    def build_statement(node_type: str, name: str | None, start: int, end: int) -> Statement:
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

    decl_starts, module_start = _split_declarations(lines, _boundaries(lines))
    decl_end_limit = (module_start - 1) if module_start is not None else n

    # (name, params_raw, start, end, header_end) — header_end is an offset into the raw
    # first line (where the matched `def name(params)` ends), used later to skip exactly
    # the header when scanning the function's own text for same-file calls.
    def_spans: list[tuple[str, str, int, int, int]] = []
    for idx, (start, keyword) in enumerate(decl_starts):
        raw_end = decl_starts[idx + 1][0] - 1 if idx + 1 < len(decl_starts) else decl_end_limit
        end = _trim_trailing_blank(lines, start, raw_end)
        first = _strip_line_comment(lines[start - 1])
        if keyword == "def":
            m = _DEF_HEADER_RE.match(lines[start - 1])  # raw line: `text` below isn't stripped
            if m:
                def_spans.append((m.group(1), m.group(2), start, end, m.end()))
            continue
        if not capture_statements:
            continue
        pattern = _IMPORT_ALIAS_RE if keyword == "import" else _LET_NAME_RE
        name_match = pattern.match(first)
        name = name_match.group(1) if name_match else None
        statements.append(build_statement(_NODE_TYPES[keyword], name, start, end))

    if capture_statements and module_start is not None:
        hi = _trim_trailing_blank(lines, module_start, n)
        statements.append(build_statement("module_expression", None, module_start, hi))

    resolve = make_resolver(
        bindings={}, local_defs={name for name, _, _, _, _ in def_spans}, path=path
    )
    functions: list[Function] = []
    for name, params_raw, start, end, header_end in def_spans:
        text = "\n".join(lines[start - 1 : end])
        func_id = disambiguate(function_id(path, name, start), seen_ids)
        functions.append(
            Function(
                id=func_id,
                parentId=fid,
                path=path,
                name=name,
                type="function",
                params=_def_params(params_raw),
                calls=_same_file_calls(lines[start - 1 : end], header_end, resolve),
                startLine=start,
                endLine=end,
            )
        )
        if not capture_statements:
            continue
        # Function has no `text` field (schemas/capture.py) -- the full body is carried on a
        # companion statement parented to it, same "full body on text" philosophy as every
        # other data-shaped capture in this parser, so the body stays findable/embeddable.
        statements.append(
            Statement(
                id=disambiguate(statement_id(path, start, 0), seen_ids),
                parentId=func_id,
                path=path,
                nodeType="function_body",
                text=text,
                startLine=start,
                endLine=end,
            )
        )
    return statements, functions
