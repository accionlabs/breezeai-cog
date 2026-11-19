"""What the shared page readers need from a Razor page: its markup without the C#, the spans of
the C# inside its scripts, and the tags Razor renders into links and forms.

**Markup.** The ``razor`` grammar gives HTML elements no attribute nodes, so links and forms are
read by the shared markup reader with the ``html`` grammar, over a copy of the page in which the
C# is blanked (same offsets): ``@code`` / ``@{ }`` blocks, directives, ``@if`` / ``@foreach``
headers, comments. The elements inside a code construct stay, and an ``@`` expression inside a
tag stays as written, so ``href="@Url.Action("Index")"`` is still seen as rendered on the server.

**Tag helpers.** Razor renders the address of these tags on the server, from the app's routes::

    <a asp-controller="Home" asp-action="Index">       → route  routeKind=navigation  (no endpoint)
    <form method="post" asp-page-handler="Save">       → api_call  POST  (no endpoint)
    <NavLink href="/counter">  (Blazor)                → route  routeKind=navigation  /counter

The address depends on the route table, so it is left empty; the tag's text keeps the target.
``method`` is taken only when written (``get`` / ``post``). A tag with a plain ``href`` /
``action`` is read by the shared rules instead.

**Scripts.** The ``@`` expressions and code blocks inside a ``<script>`` are server code: their
spans are passed to the script reader, which blanks them and leaves those URLs empty.
"""

from __future__ import annotations

from typing import Any

from tree_sitter import Node

from ..template_common.markup import is_page_link, link_endpoint, navigation
from ..template_common.script_blocks import blank

#: Razor's ``@`` expressions: kept in the markup copy (they are attribute values or text).
_EXPRESSIONS = frozenset({"razor_implicit_expression", "razor_explicit_expression"})
#: The C# that can sit inside a ``<script>``: blanked before the script is read.
_SCRIPT_CODE = _EXPRESSIONS | {"razor_block", "razor_condition", "razor_comment"}
#: The tag-helper attributes that make Razor render a tag's address.
_TARGETS = frozenset({
    "asp-action", "asp-controller", "asp-area", "asp-page", "asp-page-handler", "asp-route",
})


def markup_copy(root: Node, source: bytes) -> bytes:
    """The page with its C# blanked and its elements kept, at the same offsets."""
    buf = bytearray(source)
    _blank_code(root, buf, source)
    return bytes(buf)


def _blank_code(node: Node, buf: bytearray, source: bytes) -> None:
    for child in node.named_children:
        if child.type == "element":
            _blank_code(child, buf, source)
        elif child.type.startswith("razor_") and child.type not in _EXPRESSIONS:
            blank(buf, child.start_byte, child.end_byte)
            for element in _top_elements(child):
                buf[element.start_byte:element.end_byte] = source[element.start_byte:element.end_byte]
                _blank_code(element, buf, source)


def _top_elements(node: Node) -> list[Node]:
    """The outermost elements inside ``node`` (``@if (x) { <a>…</a> }`` → the ``<a>``)."""
    found: list[Node] = []
    stack = list(reversed(node.named_children))
    while stack:
        child = stack.pop()
        if child.type == "element":
            found.append(child)
        else:
            stack.extend(reversed(child.named_children))
    return found


def script_code_spans(root: Node) -> list[tuple[int, int]]:
    """The byte spans of the C# inside the page's ``<script>`` elements."""
    spans: list[tuple[int, int]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _SCRIPT_CODE:
            spans.append((node.start_byte, node.end_byte))
            continue
        stack.extend(node.named_children)
    return spans


def razor_tags(tag: str, attrs: dict[str, str]) -> dict[str, Any] | None:
    """Razor's own link and form tags (see the module docstring); ``None`` for any other tag."""
    if tag == "navlink":
        href = attrs.get("href")
        return navigation(link_endpoint(href)) if href is not None and is_page_link(href) else None
    if not any(a in _TARGETS or a.startswith("asp-route-") for a in attrs):
        return None
    if tag == "a" and "href" not in attrs:
        return navigation(None)
    if tag == "form" and "action" not in attrs:
        method = attrs.get("method", "").strip().lower()
        return {"semanticType": "api_call", "endpoint": None,
                "method": method.upper() if method in ("get", "post") else None}
    return None
