"""Web Forms markup pass (Step 1) — parse ``.aspx``/``.ascx``/``.master`` as File nodes.

The code-behind (``.aspx.cs``) is captured elsewhere; this pass captures the **markup** the
code-behind pairs with. A Web Forms page is HTML **plus** embedded C# code islands
(``<% … %>`` / ``<%= … %>`` / ``<%# … %>`` / the ``<%@ … %>`` directives), so — like the Vue
SFC — we build a **shadow source** that blanks the islands (spaces, newlines preserved) and
parse that with the ``html`` grammar. Byte offsets are unchanged, so every statement keeps its
true line/column. (The island *bodies* — the embedded C# — are a later step.)

Two things are captured here:

* **Server-control event wiring** — an ``On<Event>="Method"`` attribute on a server control
  (``OnClick``, ``OnRowCommand``, …) → a statement with ``handler`` = the code-behind method,
  so a page method joins to the control that invokes it (the P1/P2 ``handler`` model).
* **Server-control declarations** — an element whose tag is ``asp:``/``uc:`` prefixed or carries
  ``runat="server"`` (the page's UI components).

Ordinary HTML (``<div>``, ``class="…"``) is not emitted — no flood.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text
from .mounts import _REGISTER_SRC, _app_root, _to_repo_path, ci_resolve

_NEWLINE = 0x0A
_SPACE = 0x20
#: Any ``<% … %>`` island (scriptlet ``<% %>``, expression ``<%= %>``, data-bind ``<%# %>``,
#: directive ``<%@ %>``, encode ``<%: %>``). Blanked in the shadow so the html grammar isn't
#: derailed by embedded C#. DOTALL: an island may span lines.
_ISLAND = re.compile(rb"<%.*?%>", re.DOTALL)
#: A server-control event attribute: ``On`` + PascalCase event, value = the handler method.
_EVENT_ATTR = re.compile(r"^On[A-Z]\w*$")
#: ``<%@ Page CodeBehind="X.aspx.cs" %>`` / ``CodeFile="X.aspx.cs"`` — the code-behind ref.
_CODEBEHIND = re.compile(
    rb"<%@\s*(?:Page|Control|Master)\b[^%]*?\b(?:CodeBehind|CodeFile)\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def shadow_markup(source: bytes) -> bytes:
    """A same-length copy of ``source`` with every ``<% … %>`` island blanked to spaces
    (newlines kept), so the ``html`` grammar parses only the markup at true offsets."""
    out = bytearray(source)
    for m in _ISLAND.finditer(source):
        for i in range(m.start(), m.end()):
            if out[i] != _NEWLINE:
                out[i] = _SPACE
    return bytes(out)


def resolve_codebehind(source: bytes, aspx_path: str, repo_root: Path) -> str | None:
    """The markup's code-behind, repo-relative — from the ``CodeBehind``/``CodeFile`` directive
    (resolved beside the markup), else the sibling ``<name>.cs`` if it exists on disk. Honest-
    null when neither resolves (no guess)."""
    cur_dir = aspx_path.rsplit("/", 1)[0] if "/" in aspx_path else ""
    m = _CODEBEHIND.search(source)
    if m is not None:
        ref = m.group(1).decode("utf-8", "replace").lstrip("~/").replace("\\", "/")
        rel = f"{cur_dir}/{ref}" if cur_dir and "/" not in ref else ref
        hit = ci_resolve(repo_root, rel)
        if hit is not None:
            return hit
    sibling = f"{aspx_path}.cs"  # Enrollment.aspx → Enrollment.aspx.cs
    return sibling if (repo_root / sibling).is_file() else None


def resolve_control_mounts(source: bytes, path: str, repo_root: Path) -> list[tuple[str, int]]:
    """Host→control mounts declared in THIS markup: each ``<%@ Register Src="~/…​.ascx" %>``
    resolved to the control's ``.ascx`` markup repo-path (real on-disk casing, only when it
    exists — no dangling edge) + the directive's 1-based line. Deduped, in source order. The
    ``.ascx`` is now a File node (Step 1), so this is the host→control composition edge the
    graph was missing (765 Defect #1)."""
    app_root = _app_root(path, repo_root)
    cur_dir = posixpath.dirname(path)
    seen: set[str] = set()
    out: list[tuple[str, int]] = []
    for m in _REGISTER_SRC.finditer(source):
        rel = _to_repo_path(m.group(1).decode("utf-8", "replace"), cur_dir, app_root)
        if rel is None or not rel.lower().endswith(".ascx"):
            continue
        actual = ci_resolve(repo_root, rel)  # the .ascx markup itself (not the .cs)
        if actual is None or actual in seen:
            continue
        seen.add(actual)
        out.append((actual, source.count(b"\n", 0, m.start()) + 1))
    return out


def _tag_name(node: Node, source: bytes) -> str | None:
    tag = node.child_by_field_name("name")
    if tag is None:
        tag = next((c for c in node.named_children if c.type == "tag_name"), None)
    return node_text(tag, source) if tag is not None else None


def _start_tag(element: Node) -> Node | None:
    return next(
        (c for c in element.named_children if c.type in ("start_tag", "self_closing_tag")), None
    )


def _is_server_control(tag: str | None, start: Node, source: bytes) -> bool:
    """A server control: an ``asp:``/``uc:`` prefixed tag, or any tag with ``runat="server"``."""
    if tag and (":" in tag):
        return True
    for attr in start.named_children:
        if attr.type != "attribute":
            continue
        name = next((c for c in attr.named_children if c.type == "attribute_name"), None)
        if name is not None and node_text(name, source).lower() == "runat":
            return True
    return False


def _attr(node: Node, source: bytes) -> tuple[str, str]:
    """``(name, value)`` of an ``attribute`` node."""
    name_node = next((c for c in node.named_children if c.type == "attribute_name"), None)
    q = next((c for c in node.named_children if c.type == "quoted_attribute_value"), None)
    val_node = (
        next((c for c in q.named_children if c.type == "attribute_value"), None)
        if q is not None
        else next((c for c in node.named_children if c.type == "attribute_value"), None)
    )
    name = node_text(name_node, source) if name_node is not None else ""
    value = node_text(val_node, source) if val_node is not None else ""
    return name, value


def collect_markup_statements(
    root: Node, source: bytes, path: str, parent_id: str, seen_ids: set[str], limit: int
) -> list[Statement]:
    """Walk the (island-blanked) markup tree; emit event-wiring + server-control statements."""
    out: list[Statement] = []

    def _emit(node: Node, **fields: object) -> None:
        start, col = node.start_point[0] + 1, node.start_point[1]
        text = node_text(node, source)
        out.append(
            Statement(
                id=disambiguate(statement_id(path, start, col), seen_ids),
                parentId=parent_id,
                text=text if len(text) <= limit else text[:limit],
                startLine=start,
                endLine=node.end_point[0] + 1,
                path=path,
                **fields,  # type: ignore[arg-type]
            )
        )

    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.type == "element":
            start = _start_tag(node)
            if start is not None:
                tag = _tag_name(start, source)
                is_control = _is_server_control(tag, start, source)
                if is_control:
                    _emit(start, nodeType="element", name=tag)
                # Event wiring lives on the control's attributes (On<Event>="Method").
                for attr in start.named_children:
                    if attr.type != "attribute":
                        continue
                    name, value = _attr(attr, source)
                    if _EVENT_ATTR.match(name) and value:
                        _emit(attr, nodeType="attribute", name=name, handler=value)
        stack.extend(reversed(node.named_children))
    return out
