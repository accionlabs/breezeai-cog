"""Data-source controls: the queries and methods a page's data comes from.

::

    <asp:SqlDataSource ID="OrdersDS" runat="server"
        SelectCommand="SELECT Id, Total FROM Orders"       → query_statement  name OrdersDS
        UpdateCommand="UPDATE Orders SET …" />             → query_statement  name OrdersDS
    <asp:ObjectDataSource ID="RepoDS" TypeName="Shop.OrderRepo"
        SelectMethod="GetAll" />                           → handler GetAll, handlerPath OrderRepo.cs,
                                                             handlerLine 7

**SqlDataSource** (and ``AccessDataSource``): each ``Select`` / ``Insert`` / ``Update`` /
``Delete`` command is one ``query_statement`` on its attribute, named by the control's ``ID``. A
stored-procedure name (``SelectCommandType="StoredProcedure"``) counts too. A command written as
server code (``<%$ … %>``, ``<%= %>``) is not the query and is left out.

**ObjectDataSource**: each ``…Method`` attribute names a method of the ``TypeName`` class; the
record points at it when the repository declares that class and method exactly once (the
``csharp.method-lines`` fact), else it names the method only.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..base import ParseContext
from ..handler_sites import handler_location, locate_method
from ..treesitter import node_text
from .markup import _attr, _start_tag, _tag_name

_SQL_SOURCES = frozenset({"asp:sqldatasource", "asp:accessdatasource"})
_OBJECT_SOURCE = "asp:objectdatasource"
_COMMANDS = frozenset({"selectcommand", "insertcommand", "updatecommand", "deletecommand"})
_METHODS = frozenset({"selectmethod", "insertmethod", "updatemethod", "deletemethod",
                      "selectcountmethod"})


def collect_data_sources(
    root: Node, ctx: ParseContext, parent_id: str, seen_ids: set[str]
) -> list[Statement]:
    out: list[Statement] = []
    source, limit = ctx.source, ctx.statement_text_limit
    csharp = ctx.related_indexes.get("csharp", ctx.resolution_index)

    def emit(attr: Node, **fields: object) -> None:
        start, col = attr.start_point[0] + 1, attr.start_point[1]
        text = node_text(attr, source)
        out.append(Statement(
            id=disambiguate(statement_id(ctx.path, start, col), seen_ids),
            parentId=parent_id, nodeType=attr.type, text=text[:limit], startLine=start,
            endLine=attr.end_point[0] + 1, path=ctx.path, **fields,  # type: ignore[arg-type]
        ))

    stack = [root]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        start = _start_tag(node) if node.type == "element" else None
        tag = (_tag_name(start, source) or "").lower() if start is not None else ""
        if start is None or (tag not in _SQL_SOURCES and tag != _OBJECT_SOURCE):
            continue
        attrs = [(a, *_attr(a, source)) for a in start.named_children if a.type == "attribute"]
        values = {name.lower(): value.strip() for _, name, value in attrs}
        control_id = values.get("id") or None
        for attr, name, value in attrs:
            key, value = name.lower(), value.strip()
            if not value or "<%" in value:
                continue
            if tag in _SQL_SOURCES and key in _COMMANDS:
                emit(attr, semanticType="query_statement", name=control_id)
            elif tag == _OBJECT_SOURCE and key in _METHODS:
                site = locate_method(csharp, "csharp", values.get("typename"), value)
                line, path = handler_location(site, ctx.path)
                emit(attr, name=name, handler=value, handlerPath=path, handlerLine=line)
    return out
