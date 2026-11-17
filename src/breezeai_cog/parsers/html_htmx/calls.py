"""htmx request attributes → ``api_call`` statements.

An element carrying ``hx-get="/items"`` sends that request when it is triggered, so each request
attribute is one outbound call from the page::

    <button hx-post="/orders" hx-target="#list">Save</button>
    → api_call  framework=htmx  method=POST  endpoint=/orders  nodeType=attribute

Only the five request attributes count (``hx-get`` / ``hx-post`` / ``hx-put`` / ``hx-patch`` /
``hx-delete``, also written ``data-hx-*``); ``hx-target`` / ``hx-swap`` / ``hx-trigger`` … only
shape a request and record nothing.

``endpoint`` is the attribute value only when it is a literal absolute address — a path starting
with ``/`` or an ``http(s)://`` URL — with no template expression in it. A relative value resolves
against the page's own URL, an empty one means the current URL, and ``{{ url }}`` /
``{% url 'x' %}`` / ``${…}`` / ``@Url.Action(…)`` are rendered elsewhere: all of those keep the
call with ``endpoint`` null rather than a guessed address.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..emits import Emit, apply
from ..treesitter import node_text, parse_source

_VERBS = frozenset({"get", "post", "put", "patch", "delete"})

#: Characters that only appear in an address a template engine renders, never in a literal one.
_TEMPLATE_MARKERS = ("{", "}", "<", ">", "$")


def _verb(attribute_name: str) -> str | None:
    """``hx-post`` / ``data-hx-post`` → ``POST``; any other attribute → None."""
    name = attribute_name.lower()
    if name.startswith("data-"):
        name = name[5:]
    if not name.startswith("hx-"):
        return None
    verb = name[3:]
    return verb.upper() if verb in _VERBS else None


def _endpoint(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not (value.startswith("/") or value.startswith(("http://", "https://"))):
        return None
    if value.startswith("//") or any(m in value for m in _TEMPLATE_MARKERS):
        return None
    return value


def _value(attribute: Node, source: bytes) -> str | None:
    for child in attribute.named_children:
        if child.type == "attribute_value":
            return node_text(child, source)
        if child.type == "quoted_attribute_value":
            inner = next((c for c in child.named_children if c.type == "attribute_value"), None)
            return node_text(inner, source) if inner is not None else ""
    return None


def htmx_emits(root: Node, source: bytes) -> list[Emit]:
    """One ``api_call`` emit per htmx request attribute under ``root``, in document order."""
    found: list[Emit] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "attribute":
            name = next((c for c in node.named_children if c.type == "attribute_name"), None)
            method = _verb(node_text(name, source)) if name is not None else None
            if method is not None:
                found.append(Emit(node, {
                    "semanticType": "api_call", "framework": "htmx", "method": method,
                    "endpoint": _endpoint(_value(node, source)),
                }))
            continue
        stack.extend(reversed(node.named_children))
    return found


def detect_htmx_calls(
    record: FileRecord, markup: bytes, path: str, timeout_micros: int = 0
) -> None:
    """Append htmx ``api_call`` statements for ``markup`` to ``record`` — the markup parsers' hook
    into this detector. Only called with statement capture."""
    if b"hx-" not in markup:
        return
    root = parse_source("html", markup, timeout_micros).root_node
    emits = htmx_emits(root, markup)
    if emits:
        apply(record, emits, markup, path)
