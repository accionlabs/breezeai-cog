"""Lead a page's event wiring and user controls to the C# that declares them.

::

    <%@ Page CodeBehind="Orders.aspx.cs" Inherits="Shop.Orders" %>
    <asp:Button OnClick="Save_Click" />   → handlerPath Orders.aspx.cs, handlerLine 8
    <uc:DateField runat="server" />       → handler DateField, handlerPath Controls/DateField.ascx.cs,
                                            handlerLine 6 (the class the .ascx Inherits)

**Event handlers.** ``On<Event>="Method"`` names a method of the page class: the ``Inherits``
class in the code-behind, or a method of the page's own ``<script runat="server">``. Exactly one
declaration of that name → its location; none (a base-class or library method) or several
(overloads, a name in both places) → no location.

**User controls.** A registered control's ``.ascx`` names its class with ``Inherits`` and its
code-behind with ``CodeBehind`` / ``CodeFile``. When that file declares the class exactly once,
the control's record points at the class; otherwise it keeps its link to the ``.ascx``.

Only C# code-behind is read; a VB one leaves the location empty.
"""

from __future__ import annotations

import re
from pathlib import Path

from tree_sitter import Node

from ...emit import file_id
from ...schemas import FileRecord
from ..base import ParseContext
from ..handler_sites import handler_location
from ..razor.page_models import class_sites
from ..treesitter import node_text, parse_source
from .markup import resolve_codebehind

_INHERITS = re.compile(
    rb"<%@\s*(?:Page|Control|Master)\b[^%]*?\bInherits\s*=\s*[\"']\s*([\w.]+)\s*[\"']", re.IGNORECASE)
_EVENT_ATTR = re.compile(r"^On[A-Z]\w*$")
_CLASSES = ("class_declaration",)
_NAMESPACES = ("namespace_declaration", "file_scoped_namespace_declaration")
_METHODS = ("method_declaration",)

Site = tuple[str, int]


def _inherits(markup: bytes) -> tuple[str, str] | None:
    """``(namespace, simple name)`` of the directive's ``Inherits`` class."""
    m = _INHERITS.search(markup)
    if m is None:
        return None
    ns, _, simple = m.group(1).decode("ascii", "replace").rpartition(".")
    return ns, simple


def _code_behind(markup: bytes, rel: str, repo_root: Path) -> tuple[str, bytes, Node] | None:
    """The markup's C# code-behind: its path, source and tree."""
    cs = resolve_codebehind(markup, rel, repo_root)
    if cs is None or not cs.lower().endswith(".cs"):
        return None
    try:
        source = (repo_root / cs).read_bytes()
    except OSError:
        return None
    return cs, source, parse_source("csharp", source).root_node


def _class_methods(root: Node, source: bytes, ns: str, simple: str) -> dict[str, list[int]]:
    """Method name → the lines it is declared at, over every top-level declaration of the class
    ``ns.simple`` in this file (a partial class may be declared more than once)."""
    out: dict[str, list[int]] = {}

    def walk(node: Node, cur: str) -> None:
        for child in node.named_children:
            if child.type in _NAMESPACES:
                name = child.child_by_field_name("name")
                inner = node_text(name, source) if name is not None else ""
                walk(child, f"{cur}.{inner}" if cur and inner else inner or cur)
            elif child.type in _CLASSES:
                name = child.child_by_field_name("name")
                body = child.child_by_field_name("body")
                if (name is None or body is None or node_text(name, source) != simple
                        or (ns and cur != ns)):
                    continue
                for member in body.named_children:
                    if member.type in _METHODS and (mname := member.child_by_field_name("name")):
                        out.setdefault(node_text(mname, source), []).append(
                            member.start_point[0] + 1)
            elif child.type == "declaration_list":
                walk(child, cur)

    walk(root, "")
    return out


def _handler_sites(record: FileRecord, ctx: ParseContext) -> dict[str, list[Site]]:
    """Every method the page's event wiring can name: the code-behind class's and the page's own."""
    sites: dict[str, list[Site]] = {}
    fid = file_id(ctx.path)
    for fn in record.functions:
        if fn.parentId == fid:
            sites.setdefault(fn.name, []).append((ctx.path, fn.startLine))
    inherits = _inherits(ctx.source)
    found = _code_behind(ctx.source, ctx.path, ctx.repo_root) if inherits is not None else None
    if inherits is not None and found is not None:
        cs, source, root = found
        for name, lines in _class_methods(root, source, *inherits).items():
            sites.setdefault(name, []).extend((cs, line) for line in lines)
    return sites


def _control_class(ascx: str, repo_root: Path) -> tuple[str, Site] | None:
    """The class a user control's ``.ascx`` inherits, when its code-behind declares it once."""
    try:
        markup = (repo_root / ascx).read_bytes()
    except OSError:
        return None
    inherits = _inherits(markup)
    found = _code_behind(markup, ascx, repo_root) if inherits is not None else None
    if inherits is None or found is None:
        return None
    ns, simple = inherits
    cs, source, root = found
    lines = [line for decl_ns, line in class_sites(root, source).get(simple, ())
             if not ns or decl_ns == ns]
    return (simple, (cs, lines[0])) if len(lines) == 1 else None


def link_page_handlers(record: FileRecord, ctx: ParseContext) -> None:
    """Locate the event handlers and user-control classes of the page's records."""
    if not ctx.capture_statements:
        return
    sites: dict[str, list[Site]] | None = None
    for stmt in record.statements:
        if stmt.nodeType == "attribute" and stmt.handler and _EVENT_ATTR.match(stmt.name or ""):
            if sites is None:
                sites = _handler_sites(record, ctx)
            found = sites.get(stmt.handler, [])
            if len(found) == 1:
                stmt.handlerLine, stmt.handlerPath = handler_location(found[0], ctx.path)
        elif (stmt.nodeType == "element" and stmt.handlerPath
              and stmt.handlerPath.lower().endswith(".ascx")):
            control = _control_class(stmt.handlerPath, ctx.repo_root)
            if control is not None:
                stmt.handler, site = control
                stmt.handlerLine, stmt.handlerPath = handler_location(site, ctx.path)
