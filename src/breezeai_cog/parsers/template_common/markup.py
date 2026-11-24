"""Links and form posts in a page's markup, read the same way for every template type.

A link moves the user to another page, and a form with an ``action`` sends a request when it is
submitted::

    <a href="/about.html">About</a>                     → route  routeKind=navigation  /about.html
    <form action="/contact/send" method="post">          → api_call  POST  /contact/send

**Links.** ``<a href>`` and ``<area href>``. The address is kept only when it is a literal path
from the site root (``/about.html``), as for every other navigation record: a relative path
depends on the current page, and a value with a template expression (``{{ url }}``,
``{% url 'x' %}``, ``@Url.Action(…)``) is rendered elsewhere — both are recorded with no
address. In-page anchors (``#top``), other sites (``https://…``, ``//cdn``) and other schemes
(``mailto:``, ``javascript:``) are not navigation within the application and are not recorded.
A tag that also carries a client router's link (``routerLink``) is left to that router's reader.

**Forms.** Only a ``<form>`` with an ``action`` sends a request of its own: without one, an
Angular, AngularJS or Vue form is handled in script and a plain form posts back to its own page.
``method`` is ``GET`` when absent (the HTML default); ``dialog`` sends nothing and is skipped; a
value that is not ``get`` / ``post`` leaves ``method`` empty. The address follows the htmx rule: a
literal path from the site root or an ``http(s)://`` URL, else empty.

**Framework tags.** A parser passes ``tags``: its own rules for the tags its framework renders
into links or forms (Razor's ``<a asp-action>``, Blazor's ``<NavLink href>``). They are asked
first, with the tag name (lower case) and its attributes; ``None`` falls back to the rules above.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tree_sitter import Node

from ...schemas import FileRecord
from ..emits import Emit, apply
from ..treesitter import node_text, parse_source

_TAGS = ("start_tag", "self_closing_tag")
_LINK_TAGS = frozenset({"a", "area"})
#: Attributes that make a tag a client router's link, recorded by that router's reader.
_ROUTER_LINKS = frozenset({"routerlink", "[routerlink]"})
#: Characters that only appear in an address a template engine renders, never in a literal one.
_TEMPLATE_MARKERS = ("{", "}", "<", ">", "$", "@", "%")

#: A framework's rules for its own tags: ``(tag name, attributes)`` → the statement fields, or
#: ``None`` when the tag is not one of them.
TagRule = Callable[[str, dict[str, str]], dict[str, Any] | None]


def _attributes(tag: Node, source: bytes) -> dict[str, str]:
    """A tag's attributes, lower-cased name → value as written (``""`` for a bare attribute).
    The first of a repeated attribute wins, as in the browser."""
    found: dict[str, str] = {}
    for attr in tag.named_children:
        if attr.type != "attribute":
            continue
        name = next((c for c in attr.named_children if c.type == "attribute_name"), None)
        if name is None:
            continue
        found.setdefault(node_text(name, source).lower(), _value(attr, source))
    return found


def _value(attribute: Node, source: bytes) -> str:
    for child in attribute.named_children:
        if child.type == "attribute_value":
            return node_text(child, source)
        if child.type == "quoted_attribute_value":
            inner = next((c for c in child.named_children if c.type == "attribute_value"), None)
            return node_text(inner, source) if inner is not None else ""
    return ""


def _templated(value: str) -> bool:
    return any(m in value for m in _TEMPLATE_MARKERS)


def is_page_link(href: str) -> bool:
    """A link to a page of the application: not an anchor, another site or another scheme."""
    href = href.strip()
    if not href or href.startswith(("#", "//")):
        return False
    scheme, colon, _ = href.partition(":")
    return not (colon and scheme.isalpha() and "/" not in scheme)


def link_endpoint(href: str) -> str | None:
    href = href.strip().split("#", 1)[0]
    if not href.startswith("/") or _templated(href):
        return None
    return href


def _form_endpoint(action: str) -> str | None:
    action = action.strip()
    if not (action.startswith("/") or action.startswith(("http://", "https://"))):
        return None
    if action.startswith("//") or _templated(action):
        return None
    return action


def navigation(endpoint: str | None) -> dict[str, Any]:
    """The fields of a link's record."""
    return {"semanticType": "route", "routeKind": "navigation", "endpoint": endpoint}


def _form_method(attrs: dict[str, str]) -> tuple[bool, str | None]:
    """``(sends, method)``: whether the form sends a request, and its verb when literal."""
    method = attrs.get("method")
    if method is None:
        return True, "GET"
    method = method.strip().lower()
    if method == "dialog":
        return False, None
    return True, method.upper() if method in ("get", "post") else None


def markup_emits(
    root: Node, source: bytes, *, links: bool = True, tags: TagRule | None = None
) -> list[Emit]:
    """One emit per link (when ``links``) and per form with an ``action`` under ``root``."""
    found: list[Emit] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in _TAGS:
            emit = _emit(node, source, links, tags)
            if emit is not None:
                found.append(emit)
            continue
        stack.extend(reversed(node.named_children))
    return found


def _emit(tag: Node, source: bytes, links: bool, tags: TagRule | None) -> Emit | None:
    name = next((c for c in tag.named_children if c.type == "tag_name"), None)
    if name is None:
        return None
    tag_name = node_text(name, source).lower()
    attrs = _attributes(tag, source)
    fields = tags(tag_name, attrs) if tags is not None else None
    if fields is not None:
        return None if fields.get("routeKind") == "navigation" and not links else Emit(tag, fields)
    if tag_name in _LINK_TAGS:
        href = attrs.get("href")
        if not links or href is None or not is_page_link(href) or _ROUTER_LINKS & attrs.keys():
            return None
        return Emit(tag, navigation(link_endpoint(href)))
    if tag_name == "form" and "action" in attrs:
        sends, method = _form_method(attrs)
        if not sends:
            return None
        return Emit(tag, {"semanticType": "api_call", "method": method,
                          "endpoint": _form_endpoint(attrs["action"])})
    return None


def read_page_markup(
    record: FileRecord,
    markup: bytes,
    path: str,
    timeout_micros: int = 0,
    *,
    links: bool = True,
    tags: TagRule | None = None,
    source: bytes | None = None,
) -> None:
    """Append the page's links (when ``links``; off for fixture files, like every route) and
    form posts to ``record``. ``markup`` is the page, or a copy with its server code blanked
    at the same offsets; ``source`` (the page) then gives the records their text. Only called
    with statement capture."""
    lowered = markup.lower()
    if b"href" not in lowered and b"<form" not in lowered and b"asp-" not in lowered:
        return
    root = parse_source("html", markup, timeout_micros).root_node
    emits = markup_emits(root, markup, links=links, tags=tags)
    if emits:
        apply(record, emits, source if source is not None else markup, path)
