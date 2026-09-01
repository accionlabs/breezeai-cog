"""Akka / Pekko messaging detection (``ref ! msg`` / ``ref ? msg`` / receive handlers).

Wired into the base ``ScalaParser.extract`` — not ``scala_play`` — so an Akka-using Play
controller keeps both its routes and its messaging.

Pekko does not contain the substring "akka" (its root package is ``org.apache.pekko``),
so the byte guard checks both spellings. Unary ``!x`` is a ``prefix_expression``, a
different node type from the binary ``infix_expression`` matched here — requiring the
binary form structurally excludes it, verified against the actual
``tree-sitter-scala`` grammar.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..base import ParseContext
from ..treesitter import node_text

_TELL_OR_ASK = {"!": "tell", "?": "ask"}


def _is_receive_handler(fn_node: Node, source: bytes) -> bool:
    """``def receive: Receive = { case … }`` — verified: the grammar puts the
    ``case_block`` directly on the function's ``body`` field, no wrapping ``block``."""
    name = fn_node.child_by_field_name("name")
    body = fn_node.child_by_field_name("body")
    return (
        name is not None
        and node_text(name, source) == "receive"
        and body is not None
        and body.type == "case_block"
    )


def _is_receive_message_call(call: Node, source: bytes) -> bool:
    """``Behaviors.receiveMessage { … }`` (with or without a type argument) — verified:
    the ``case_block`` lands on the call's ``arguments`` field."""
    fn = call.child_by_field_name("function")
    if fn is not None and fn.type == "generic_function":
        fn = fn.child_by_field_name("function")
    if fn is None:
        return False
    args = call.child_by_field_name("arguments")
    return args is not None and args.type == "case_block" and node_text(fn, source) == "Behaviors.receiveMessage"


def detect_scala_events(
    root: Node, ctx: ParseContext, parent_id: str, seen_ids: set[str]
) -> list[Statement]:
    if not ctx.capture_statements:
        return []
    source = ctx.source
    if b"akka" not in source and b"pekko" not in source:
        return []
    events: list[Statement] = []

    def emit(node: Node, semantic: str, method: str | None) -> None:
        start = node.start_point[0] + 1
        events.append(Statement(
            id=disambiguate(statement_id(ctx.path, start, node.start_point[1]), seen_ids),
            parentId=parent_id,
            nodeType=node.type,
            semanticType=semantic,
            text=node_text(node, source),
            method=method,
            framework="akka",
            startLine=start,
            endLine=node.end_point[0] + 1,
            path=ctx.path,
        ))

    def walk(n: Node) -> None:
        for c in n.named_children:
            if c.type == "infix_expression":
                op = c.child_by_field_name("operator")
                left = c.child_by_field_name("left")
                right = c.child_by_field_name("right")
                if op is not None and left is not None and right is not None:
                    method = _TELL_OR_ASK.get(node_text(op, source))
                    if method is not None:
                        emit(c, "eventbus_send", method)
            elif c.type in ("function_definition", "function_declaration") and _is_receive_handler(c, source):
                emit(c, "eventbus_consumer", None)
            elif c.type == "call_expression" and _is_receive_message_call(c, source):
                emit(c, "eventbus_consumer", None)
            walk(c)

    walk(root)
    return events
