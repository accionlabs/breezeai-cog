"""Razor ``.cshtml`` / ``.razor`` template capture.

The ``razor`` grammar parses the whole file (markup + embedded C#) in one pass, so every
statement keeps its genuine grammar ``nodeType`` — no shadow, no ``synthetic``. Mirrors the
Angular/Vue/Web-Forms template capture:

* ``@page "/orders/{id}"`` → a ``route`` statement (``routeKind=page``), endpoint = the path,
  ``handler`` = the page: a Blazor component (``.razor``) is its file's component
  (``Counter.razor`` → ``Counter``, ``handlerLine`` 1: the component is the whole file); a
  Razor Page (``.cshtml``) is its ``@model`` PageModel class (declared in another file, so no
  ``handlerLine``); neither → empty.
* ``@onclick="Remove"`` (Blazor event) → the event on ``name``, the referenced method on
  ``handler`` — the P1–P3 handler model.
* ``@Model.Title`` / ``@expr`` → an interpolation-like reference (``semanticType=null``, like
  Angular/Vue ``{{ }}``), the expression on ``name``.
* ``@foreach`` / ``@if`` → control-flow blocks (``semanticType=null``).
* ``@model OrderModel`` → the view's model type on ``name``, as written (``Order[]``,
  ``Order?`` — the grammar stops at ``Order``, so the rest of the line is read from the source).

A Razor Page (``.cshtml``) is served at its place under the nearest ``Pages/`` folder
(``Areas/<area>/Pages/`` → ``/<area>/…``; ``Index`` → its folder): a bare ``@page`` gets that
address, a relative template (``@page "{id:int}"``) is joined to it, an absolute one
(``@page "/x"``) is kept. Outside ``Pages/`` the address is unknown (empty). A file is a page
only through its ``@page`` directive — ``_Layout`` / ``_ViewImports`` have none, while
Blazor Server's ``_Host.cshtml`` (``@page "/"``) is a real one. The grammar has no node for a bare ``@page``
(it is an ``ERROR`` holding ``@`` + the ``at_page`` keyword), so that record is ``synthetic``,
anchored to the directive's span.

The ``@code { }`` block's C# methods are a separate concern (Step 2), like Vue's ``<script>``.
"""

from __future__ import annotations

import re

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text


def _descendant(node: Node, kind: str) -> Node | None:
    """First descendant (inclusive) of ``node`` whose type is ``kind`` (pre-order)."""
    if node.type == kind:
        return node
    for c in node.named_children:
        hit = _descendant(c, kind)
        if hit is not None:
            return hit
    return None


def _page_template(node: Node, source: bytes) -> str | None:
    """The route template of an ``@page`` directive (``@page "/x"`` → ``/x``); None for none."""
    lit = _descendant(node, "string_literal_content")
    return node_text(lit, source) if lit is not None else None


def _is_bare_page(node: Node) -> bool:
    """A bare ``@page``: the grammar parses it as an ``ERROR`` of exactly ``@`` + ``at_page``."""
    return node.type == "ERROR" and [c.type for c in node.children] == ["@", "at_page"]


def _page_address(path: str) -> str | None:
    """The address a Razor Page is served at, from its place under the nearest ``Pages/``
    folder: ``Pages/Orders/Edit.cshtml`` → ``/Orders/Edit``, ``Pages/Orders/Index.cshtml`` →
    ``/Orders``, ``Areas/Admin/Pages/Users.cshtml`` → ``/Admin/Users``. None outside ``Pages/``."""
    parts = path.split("/")
    folders = parts[:-1]
    if "Pages" not in folders:
        return None
    i = len(folders) - 1 - folders[::-1].index("Pages")
    area = [parts[i - 1]] if i >= 2 and parts[i - 2] == "Areas" else []
    stem = parts[-1].rsplit(".", 1)[0]
    segments = area + parts[i + 1:-1] + ([] if stem.lower() == "index" else [stem])
    return "/" + "/".join(segments)


def _page_endpoint(template: str | None, path: str) -> str | None:
    """The page's endpoint. A Blazor template is the whole address. A Razor Page's absolute
    template (``/x``, or ``~/x`` — ASP.NET reads both as an override of the page's address and
    serves ``~/x`` at ``/x``) is kept; none, or a relative one (``{id:int}``), is joined to the
    page's own address — empty when that is unknown, never the bare template."""
    if path.lower().endswith(".razor") or (template or "").startswith("/"):
        return template
    if (template or "").startswith("~/"):
        return (template or "")[1:]
    if (template or "").startswith("~"):
        return None  # ``~`` / ``~x``: no override and no valid relative route — empty, not a guess
    address = _page_address(path)
    if address is None or not template:
        return address
    return address.rstrip("/") + "/" + template


def _attr_name(node: Node, source: bytes) -> str:
    """A ``razor_html_attribute``'s name without the leading ``@`` (``@onclick`` → ``onclick``)."""
    name = _descendant(node, "razor_attribute_name")
    return node_text(name, source).lstrip("@") if name is not None else ""


def _attr_handler(node: Node, source: bytes) -> str | None:
    """The referenced member of an event attribute — the first identifier in its value
    (``@onclick="Remove"`` → ``Remove``); ``None`` when the value has no identifier."""
    value = _descendant(node, "razor_attribute_value")
    ident = _descendant(value, "identifier") if value is not None else None
    return node_text(ident, source) if ident is not None else None


#: A plain or dotted class name (``global::`` allowed) — the only kind of ``@model`` type that
#: can be a page's own class.
_CLASS_NAME = re.compile(r"(?:global::)?[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def _model_type(node: Node, source: bytes) -> str | None:
    """The ``@model`` type as written: the rest of the directive's line from the type on, so
    ``Order[]`` / ``Order?`` keep the marker the grammar stops before (``List<Order>`` too)."""
    model = next(iter(node.named_children), None)
    if model is None:
        return None
    end = source.find(b"\n", model.start_byte)
    line = source[model.start_byte:end if end != -1 else len(source)]
    text = line.split(b"@*", 1)[0].decode("utf-8", "replace").strip()  # a trailing @* *@ comment
    return text or None


def _model_name(node: Node, source: bytes) -> str | None:
    """The ``@model`` class as written — ``OrdersModel`` / ``Shop.Pages.OrdersModel``. Any other
    type (``List<Order>``, ``Shop.OrdersModel<T>``, ``Order[]``, ``Order?``) is no page's own
    class: None, never part of it."""
    text = _model_type(node, source)
    return text if text is not None and _CLASS_NAME.fullmatch(text) else None


def _page_handler(root: Node, source: bytes, path: str) -> str | None:
    """The page an ``@page`` directive serves (see the module docstring)."""
    if path.lower().endswith(".razor"):
        stem = path.rsplit("/", 1)[-1][: -len(".razor")]
        return stem if stem.isidentifier() else None
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "razor_model_directive":
            return _model_name(node, source)
        stack.extend(node.named_children)
    return None


def collect_razor_statements(
    root: Node,
    source: bytes,
    path: str,
    parent_id: str,
    seen_ids: set[str],
    limit: int,
    *,
    framework: str = "razor",
    emit_routes: bool = True,
) -> list[Statement]:
    """Walk the razor tree and emit one flat Statement per template construct."""
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

    is_razor = path.lower().endswith(".razor")
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        t = node.type
        bare = not is_razor and _is_bare_page(node)
        if t == "razor_page_directive" or bare:
            if emit_routes:
                _emit(
                    node,
                    nodeType="synthetic" if bare else t,  # no grammar node for a bare @page
                    semanticType="route",
                    framework=framework,
                    routeKind="page",
                    endpoint=_page_endpoint(None if bare else _page_template(node, source), path),
                    handler=(handler := _page_handler(root, source, path)),
                    # A Blazor component is its whole file: the handler starts at line 1.
                    handlerLine=1 if handler and is_razor else None,
                )
        elif t == "razor_implicit_expression" and not source[node.start_byte:].startswith(b"@"):
            continue  # a grammar artifact (`@model Order[]` leaves `[]…` behind), not an `@expr`
        elif t == "razor_html_attribute":
            name = _attr_name(node, source)
            # Blazor event handlers are @on<event>; others (@bind, @ref) are plain bindings.
            handler = _attr_handler(node, source) if name.startswith("on") else None
            _emit(node, nodeType=t, name=name, handler=handler)
        elif t == "razor_implicit_expression":
            _emit(node, nodeType=t, name=node_text(node, source).lstrip("@").strip())
        elif t == "razor_model_directive":
            _emit(node, nodeType=t, name=_model_type(node, source))
        elif t in ("razor_foreach", "razor_if"):
            _emit(node, nodeType=t)  # control flow — semanticType null
        stack.extend(reversed(node.named_children))
    return out
