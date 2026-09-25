"""Razor ``.cshtml`` / ``.razor`` template capture.

The ``razor`` grammar parses the whole file (markup + embedded C#) in one pass, so every
statement keeps its genuine grammar ``nodeType`` — no shadow, no ``synthetic``. Mirrors the
Angular/Vue/Web-Forms template capture:

* ``@page "/orders/{id}"`` → a ``route`` statement (``routeKind=page``), endpoint = the path.
* ``@onclick="Remove"`` (Blazor event) → the event on ``name``, the referenced method on
  ``handler`` — the P1–P3 handler model.
* ``@Model.Title`` / ``@expr`` → an interpolation-like reference (``semanticType=null``, like
  Angular/Vue ``{{ }}``), the expression on ``name``.
* ``@foreach`` / ``@if`` → control-flow blocks (``semanticType=null``).
* ``@model OrderModel`` → the view's model type on ``name``.

The ``@code { }`` block's C# methods are a separate concern (Step 2), like Vue's ``<script>``.
"""

from __future__ import annotations

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


def _page_endpoint(node: Node, source: bytes) -> str | None:
    """The route path of an ``@page`` directive — the string literal (``@page "/x"`` → ``/x``);
    ``None`` for a bare ``@page`` (Blazor allows a path-less page)."""
    lit = _descendant(node, "string_literal_content")
    return node_text(lit, source) if lit is not None else None


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


def _model_name(node: Node, source: bytes) -> str | None:
    ident = _descendant(node, "identifier")
    return node_text(ident, source) if ident is not None else None


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

    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        t = node.type
        if t == "razor_page_directive":
            if emit_routes:
                _emit(
                    node,
                    nodeType=t,
                    semanticType="route",
                    framework=framework,
                    routeKind="page",
                    endpoint=_page_endpoint(node, source),
                )
        elif t == "razor_html_attribute":
            name = _attr_name(node, source)
            # Blazor event handlers are @on<event>; others (@bind, @ref) are plain bindings.
            handler = _attr_handler(node, source) if name.startswith("on") else None
            _emit(node, nodeType=t, name=name, handler=handler)
        elif t == "razor_implicit_expression":
            _emit(node, nodeType=t, name=node_text(node, source).lstrip("@").strip())
        elif t == "razor_model_directive":
            _emit(node, nodeType=t, name=_model_name(node, source))
        elif t in ("razor_foreach", "razor_if"):
            _emit(node, nodeType=t)  # control flow — semanticType null
        stack.extend(reversed(node.named_children))
    return out
