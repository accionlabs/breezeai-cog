"""Java syntax-tree reads the Vert.x detectors share (``events.py``, ``legacy.py``): the class of a
``new X(…)`` handler, an anonymous handler's ``handle``, an event-bus address, and where a handler
class's ``handle`` is. The plain Java reads they also use live in ``parsers/java/syntax.py``."""

from __future__ import annotations

from tree_sitter import Node

from ..addresses import informative, mark_runtime, runtime_name
from ..handler_sites import handler_location, locate_method
from ..java.syntax import (arguments, calls_and_creations, imports_and_package, invocations,
                           java_string, known_address, local_value, name_of, plain,
                           request_address, value_source)
from ..treesitter import node_text
from ..vertx_common import render_address

__all__ = [  # the plain Java reads live in the Java package; re-exported for the Vert.x modules
    "arguments", "calls_and_creations", "imports_and_package", "invocations", "java_string",
    "known_address", "local_value", "name_of", "plain", "request_address", "value_source",
    "anonymous_handle", "event_address", "handle_location", "handler_class",
]


def handler_class(arg: Node | None, source: bytes) -> str | None:
    """The class of a handler given as ``new LoadHandler(…)`` (no anonymous body) — passed
    directly or through a local assigned once in the enclosing method — else None."""
    if arg is not None and arg.type == "identifier":
        arg = local_value(arg, node_text(arg, source), source)
    if arg is None or arg.type != "object_creation_expression" \
            or any(c.type == "class_body" for c in arg.named_children):
        return None
    typ = arg.child_by_field_name("type")
    return node_text(typ, source).split("<", 1)[0] if typ is not None else None


def anonymous_handle(arg: Node | None, source: bytes) -> int | None:
    """The declaration line of ``handle`` in a handler given as an anonymous class
    (``new Handler<Message>() { public void handle(Message m) { … } }``): every Vert.x handler
    type (``Handler``, ``AsyncResultHandler``, ``VoidHandler``) has one method, ``handle``, so
    that method is the code that runs. It is the start line of the anonymous class's ``handle``
    Function (annotations included). None when ``arg`` is no anonymous class, or its body
    declares no ``handle`` or more than one."""
    if arg is None or arg.type != "object_creation_expression":
        return None
    body = next((c for c in arg.named_children if c.type == "class_body"), None)
    if body is None:
        return None
    handles = [m for m in body.named_children if m.type == "method_declaration"
               and (nm := m.child_by_field_name("name")) is not None
               and node_text(nm, source) == "handle"]
    return handles[0].start_point[0] + 1 if len(handles) == 1 else None


def event_address(node: Node, source: bytes, consts: dict[str, str]) -> str | None:
    """An event-bus address: its value when it folds to a constant, else every part read at
    runtime marked ``${…}`` (``addresses.py``: ``${config:api-verticle.address}``,
    ``${prefix}.orders``); None when it says nothing."""
    value = render_address(node, source, consts, java_string)
    if value is None:
        value = "{" + runtime_name(node_text(value_source(node, source), source)) + "}"
    return informative(mark_runtime(value))


def handle_location(index: object | None, impl: str | None, path: str
                    ) -> tuple[int | None, str | None]:
    """(handlerLine, handlerPath) of class ``impl``'s ``handle`` method, when the repo has it."""
    found = locate_method(index, "java", impl, "handle") if impl else None
    return handler_location(found, path)
