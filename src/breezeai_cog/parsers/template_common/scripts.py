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

**Scope.** The classic blocks are one program; each ``type="module"`` block is a program of its
own. A URL held in a page constant is followed within its program::

    <script>var API = '/api/orders';</script>
    <script>$.post(API);</script>          → api_call  POST  /api/orders

Only a name declared once, at the top of the program, to a plain string, and never assigned or
bound again (a parameter, a pattern, a function of that name) counts; anything else stays empty.

**Script files.** :func:`link_page_scripts` turns ``<script src>`` into a file link when the file
is in the repository: a relative ``src`` against the page's folder, a ``/``-rooted one against the
page's folder or any folder above it, linked only when exactly one of those files exists.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath

from tree_sitter import Node

from ...emit import disambiguate, file_id
from ...schemas import FileRecord, Statement
from ..base import ParseContext
from ..enclosing import Enclosing
from ..treesitter import node_text, parse_source
from ..typescript.parser import TypeScriptParser
from ..typescript_jquery.calls import detect_inline_jquery
from .script_blocks import blank, script_programs, script_sources

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
#: ``{{ … }}`` / ``{% … %}``: Twig's, and in a ``.html`` page Django's, Jinja's or Handlebars'.
#: Browsers never fill these in inside ``<script>``, so there they are always a template value.
_CURLY_EXPR = re.compile(r"\{\{|\{%")


def _server_expr(path: str) -> re.Pattern[str]:
    """The server-expression pattern for ``path``'s dialect: ``@`` is only Razor's; ``{{`` is
    Twig's, or a template engine's in a ``.html`` page (in another page it is plain JavaScript
    text)."""
    low = path.lower()
    if low.endswith((".cshtml", ".razor", ".vbhtml")):
        return re.compile(f"{_SERVER_TAG.pattern}|{_RAZOR_EXPR.pattern}")
    if low.endswith((".twig", ".html", ".htm")):
        return re.compile(f"{_SERVER_TAG.pattern}|{_CURLY_EXPR.pattern}")
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
    server_lines = _lines_of(ctx.source, server_code)
    server_expr = _server_expr(ctx.path)
    for program in script_programs(ctx.source, ctx.path):
        buf = bytearray(program)
        for start, end in server_code:
            blank(buf, start, end)
        shadow = bytes(buf)
        root = parse_source("typescript", shadow, ctx.parse_timeout_micros).root_node
        # The script is read on its own: the page's index is not the TypeScript one.
        script_ctx = replace(ctx, source=shadow, resolution_index=None, related_indexes={})
        found = TypeScriptParser().extract(root, script_ctx)
        _merge(record, found.statements, ctx.source, shadow, server_lines, server_expr)
        _fold_constants(record, root, ctx.source, server_lines, server_expr)


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


# ── page constants ───────────────────────────────────────────────────────────────────────────

#: Calls whose first argument is the URL (``fetch(url)``, ``$.post(url)``, ``axios.get(url)``…),
#: or whose first argument is a settings object with a ``url`` (``$.ajax({url})``, ``axios({url})``).
_URL_FIRST = frozenset({
    "fetch", "axios", "get", "post", "put", "patch", "delete", "head", "options", "request",
    "ajax", "getJSON", "getScript", "load",
})
#: Nodes that bind a name of their own: a name found inside one is not a page constant.
_BINDS = frozenset({"formal_parameters", "object_pattern", "array_pattern", "catch_clause"})
_DECLARES = frozenset({
    "function_declaration", "generator_function_declaration", "class_declaration",
    "function_expression", "class",
})


def _walk(node: Node) -> Iterator[Node]:
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.named_children))


def _plain_string(node: Node | None, source: bytes) -> str | None:
    """A string literal with no escapes, as written; else None."""
    if node is None or node.type != "string":
        return None
    parts = node.named_children
    if any(c.type != "string_fragment" for c in parts) or len(parts) > 1:
        return None
    return node_text(parts[0], source) if parts else ""


def _page_constants(
    root: Node, source: bytes, server_lines: set[int], server_expr: re.Pattern[str]
) -> dict[str, str]:
    """Names declared once at the top of the program to a plain string, never bound again. The
    shadow keeps every offset, so the value is read from the page (``source``) as written."""
    declared: Counter[str] = Counter()
    rebound: set[str] = set()
    values: dict[str, str] = {}
    for node in _walk(root):
        kind = node.type
        if kind == "variable_declarator":
            name = node.child_by_field_name("name")
            if name is None or name.type != "identifier":
                continue  # a pattern: its names are collected below
            text = node_text(name, source)
            declared[text] += 1
            decl = node.parent
            value = _plain_string(node.child_by_field_name("value"), source)
            if (decl is not None and decl.parent is not None and decl.parent.type == "program"
                    and value is not None and node.start_point[0] + 1 not in server_lines
                    and not server_expr.search(value)):
                values[text] = value
        elif kind in ("assignment_expression", "augmented_assignment_expression"):
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                rebound.add(node_text(left, source))
        elif kind == "update_expression":
            arg = node.child_by_field_name("argument")
            if arg is not None and arg.type == "identifier":
                rebound.add(node_text(arg, source))
        elif kind in _BINDS:
            rebound.update(node_text(n, source) for n in _walk(node)
                           if n.type in ("identifier", "shorthand_property_identifier_pattern"))
        elif kind == "arrow_function":
            param = node.child_by_field_name("parameter")
            if param is not None and param.type == "identifier":
                rebound.add(node_text(param, source))
        elif kind in _DECLARES:
            name = node.child_by_field_name("name")
            if name is not None:
                rebound.add(node_text(name, source))
    return {n: v for n, v in values.items() if declared[n] == 1 and n not in rebound}


def _callee_name(call: Node, source: bytes) -> str | None:
    callee = call.child_by_field_name("function")
    if callee is not None and callee.type == "member_expression":
        callee = callee.child_by_field_name("property")
    return node_text(callee, source) if callee is not None else None


def _url_argument(call: Node, source: bytes) -> Node | None:
    """The first argument, or the ``url`` of a settings object passed first."""
    args = call.child_by_field_name("arguments")
    first = args.named_children[0] if args is not None and args.named_children else None
    if first is not None and first.type == "object":
        for pair in first.named_children:
            key = pair.child_by_field_name("key") if pair.type == "pair" else None
            if key is not None and node_text(key, source).strip("'\"") == "url":
                return pair.child_by_field_name("value")
        return None
    return first


def _in_arguments(node: Node) -> bool:
    """Whether ``node`` is passed into another call (``fetch(cache.get(K))``: ``cache.get``)."""
    parent = node.parent
    while parent is not None and parent.type not in ("expression_statement", "program"):
        if parent.type == "arguments":
            return True
        parent = parent.parent
    return False


#: An endpoint that is one whole value and nothing else (``{API}``).
_WHOLE_VALUE = re.compile(r"\{[A-Za-z_$][\w$]*\}")


def _fold_constants(
    record: FileRecord,
    root: Node,
    source: bytes,
    server_lines: set[int],
    server_expr: re.Pattern[str],
) -> None:
    """Give an ``api_call`` the page constant its URL argument names, when its endpoint is empty
    or only that name as a value read at runtime (``$.post(API)`` → ``{API}``)."""
    consts = _page_constants(root, source, server_lines, server_expr)
    if not consts:
        return
    calls: dict[int, list[Node]] = {}
    for node in _walk(root):
        if node.type == "call_expression" and not _in_arguments(node):
            calls.setdefault(node.start_point[0] + 1, []).append(node)
    for stmt in record.statements:
        if stmt.semanticType != "api_call" or (
                stmt.endpoint is not None and not _WHOLE_VALUE.fullmatch(stmt.endpoint)):
            continue
        if any(line in server_lines for line in range(stmt.startLine, stmt.endLine + 1)):
            continue
        found = set()
        for call in calls.get(stmt.startLine, ()):
            arg = _url_argument(call, source) if _callee_name(call, source) in _URL_FIRST else None
            if (arg is not None and arg.type == "identifier" and node_text(call, source) in stmt.text
                    and node_text(arg, source) in consts
                    and stmt.endpoint in (None, "{" + node_text(arg, source) + "}")):
                found.add(consts[node_text(arg, source)])
        if len(found) == 1:
            stmt.endpoint = found.pop()


# ── script files ─────────────────────────────────────────────────────────────────────────────

#: A ``src`` that is not a repository path: a scheme (``https:``, ``data:``), protocol-relative
#: (``//cdn``), or a value rendered on the server (``@Url.Content(…)``, ``<%= %>``, ``{{ }}``).
_NOT_A_PATH = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.\-]*:|//)|[{}<>$@%\\]")


def link_page_scripts(record: FileRecord, ctx: ParseContext) -> None:
    """Add each ``<script src>`` that is a file of the repository to ``record.importFiles``."""
    root = Path(ctx.repo_root).resolve()
    page_dir = PurePosixPath(ctx.path).parent
    for src in script_sources(ctx.source, ctx.path):
        target = _script_file(src, page_dir, root)
        if target is not None and target != ctx.path and target not in record.importFiles:
            record.importFiles.append(target)


def _script_file(src: str, page_dir: PurePosixPath, root: Path) -> str | None:
    src = src.split("?", 1)[0].split("#", 1)[0].strip()
    if not src or _NOT_A_PATH.search(src):
        return None
    if src.startswith("/"):
        bases = [page_dir, *page_dir.parents]
        src = src.lstrip("/")
    else:
        bases = [page_dir]
    found = {hit for base in bases if (hit := _repo_file(root, base / src)) is not None}
    return found.pop() if len(found) == 1 else None


def _repo_file(root: Path, rel: PurePosixPath) -> str | None:
    """``rel`` as a normalised repository path when it is a file inside ``root``; else None."""
    target = (root / rel).resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return None
    return target.relative_to(root).as_posix()
