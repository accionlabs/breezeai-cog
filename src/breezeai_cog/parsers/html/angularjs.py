"""AngularJS event directives in a template → one statement each, with the method they call.

An AngularJS event directive's value is an expression, so it is read as JavaScript, and the
method its first call names is the ``handler`` — the same record as an Angular event binding::

    <form ng-submit="vm.save()">            → nodeType=attribute  name=ng-submit  handler=save
    <button data-ng-click="remove(o)">      → nodeType=attribute  name=data-ng-click  handler=remove
    <a ng-click="vm.open = true">           → nodeType=attribute  name=ng-click  (no call: no handler)

Only AngularJS's own event directives count (:data:`_EVENTS`); the name is kept as written.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord
from ..emits import Emit, apply
from ..treesitter import node_text, parse_source

#: AngularJS's built-in event directives, without the ``ng-`` prefix.
_EVENTS = frozenset({
    "blur", "change", "click", "copy", "cut", "dblclick", "focus", "keydown", "keypress",
    "keyup", "mousedown", "mouseenter", "mouseleave", "mousemove", "mouseover", "mouseup",
    "paste", "submit",
})


def _is_event(name: str) -> bool:
    name = name.lower()
    if name.startswith("data-"):
        name = name[5:]
    return name.startswith("ng-") and name[3:] in _EVENTS


def _value(attribute: Node, source: bytes) -> str | None:
    for child in attribute.named_children:
        if child.type == "attribute_value":
            return node_text(child, source)
        if child.type == "quoted_attribute_value":
            inner = next((c for c in child.named_children if c.type == "attribute_value"), None)
            return node_text(inner, source) if inner is not None else ""
    return None


def _handler(expression: str, timeout_micros: int) -> str | None:
    """The method the first call of ``expression`` names (``vm.save()`` → ``save``); None when
    it makes no call, or calls something that is not a name."""
    if not expression.strip():
        return None
    source = expression.encode()
    stack = [parse_source("javascript", source, timeout_micros).root_node]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None and callee.type == "member_expression":
                callee = callee.child_by_field_name("property")
            if callee is not None and callee.type in ("identifier", "property_identifier"):
                return node_text(callee, source)
            return None
        stack.extend(reversed(node.named_children))
    return None


def angularjs_event_emits(root: Node, source: bytes, timeout_micros: int = 0) -> list[Emit]:
    found: list[Emit] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "attribute":
            name = next((c for c in node.named_children if c.type == "attribute_name"), None)
            if name is not None and _is_event(node_text(name, source)):
                value = _value(node, source)
                found.append(Emit(node, {
                    "name": node_text(name, source),
                    "handler": _handler(value, timeout_micros) if value is not None else None,
                }))
            continue
        stack.extend(reversed(node.named_children))
    return found


def read_angularjs_events(
    record: FileRecord, markup: bytes, path: str, timeout_micros: int = 0
) -> None:
    """Append the event directives of the AngularJS template ``markup`` to ``record``. Only
    called with statement capture."""
    if b"ng-" not in markup.lower():
        return
    root = parse_source("html", markup, timeout_micros).root_node
    emits = angularjs_event_emits(root, markup, timeout_micros)
    if emits:
        apply(record, emits, markup, path)
