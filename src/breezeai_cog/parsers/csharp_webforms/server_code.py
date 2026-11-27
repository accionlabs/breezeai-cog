"""The C# written inside a Web Forms page, read as C#.

The splitter (:mod:`..template_common.server_code`, ASP.NET dialect) finds the page's code
islands; their code, at its true offsets, is parsed with the ``c_sharp`` grammar, which accepts
statements and methods at the top level::

    <script runat="server">                      → Function  Page_Load (parented to the page)
      void Page_Load(object s, EventArgs e) { … }   and the statements in its body
    </script>
    <% if (User.IsAdmin) { %> … <% } %>          → if_statement at that line

An expression (``<%= x %>``, ``<%: x %>``, ``<%# x %>``) is wrapped as ``_= x ;`` so it parses as a
statement, the way JSP wraps its expressions; its statement's ``text`` is the expression as the
page writes it (``<%# Eval("Id") %>``), never the wrapper. Comments, directives and ``<%$ %>``
expression builders are not code.

Only a C# page is read: a page whose ``Language`` is VB, or whose code-behind is a ``.vb`` file,
is left alone (its code is not C#)."""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import file_id
from ...schemas import FileRecord
from ..base import ParseContext
from ..csharp.functions import build_method
from ..csharp.statements import extract_statements
from ..template_common.server_code import (
    ASPNET, CODE_KINDS, Island, expressions_as_written, shadow_code,
)
from ..treesitter import parse_source

#: Members of the page class: a method in ``<script runat="server">`` parses as a local function.
_METHODS = frozenset({"local_function_statement", "method_declaration", "constructor_declaration"})
#: ``<%@ Page Language="VB" %>`` / ``CodeBehind="X.aspx.vb"`` — a VB page.
_VB_PAGE = re.compile(
    rb"<%@\s*(?:Page|Control|Master)\b[^%]*?\b(?:Language\s*=\s*[\"']\s*vb\b"
    rb"|(?:CodeBehind|CodeFile)\s*=\s*[\"'][^\"']*\.vb[\"'])",
    re.IGNORECASE,
)


def is_csharp_page(source: bytes) -> bool:
    return _VB_PAGE.search(source) is None


def read_server_code(
    record: FileRecord, ctx: ParseContext, islands: list[Island], seen_ids: set[str]
) -> None:
    """Add the page's server-code methods to ``record.functions`` and, with statement capture,
    the statements of its code to ``record.statements``."""
    if not any(i.kind in CODE_KINDS for i in islands) or not is_csharp_page(ctx.source):
        return
    code = shadow_code(ctx.source, islands, ASPNET)
    root = parse_source("csharp", code, ctx.parse_timeout_micros).root_node
    fid = file_id(ctx.path)
    capture, limit = ctx.capture_statements, ctx.statement_text_limit
    methods: list[Node] = [
        member for top in root.named_children if top.type == "global_statement"
        for member in top.named_children if member.type in _METHODS
    ] + [top for top in root.named_children if top.type in _METHODS]
    for method in methods:
        functions, statements = build_method(
            method, code, ctx.path, parent_id=fid, class_name=None, seen_ids=seen_ids,
            capture=capture, limit=limit,
        )
        record.functions.extend(functions)
        record.statements.extend(statements)
    top = extract_statements(
        root, code, ctx.path, parent_id=fid, capture=capture, limit=limit, seen_ids=seen_ids,
        barriers=frozenset((m.start_byte, m.end_byte) for m in methods),
    )
    expressions_as_written(top, ctx.source, islands, ASPNET, limit)
    record.statements.extend(top)
