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
from ...schemas import FileRecord, Statement
from ...schemas.enums import SemanticType
from ..base import ParseContext
from ..treesitter import node_text
from .functions import type_map
from .statements import find_enclosing_parent_id


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
    root: Node, ctx: ParseContext, record: FileRecord
) -> None:
    if not ctx.capture_statements:
        return
    source = ctx.source
    if b"akka" not in source and b"pekko" not in source:
        return
    seen_ids = {s.id for s in record.statements}
    declared_types = type_map(root, source)
    found_any = False

    def is_actor_ref(left: Node) -> bool:
        if left.type != "identifier":
            return False
        declared = declared_types.get(node_text(left, source), "")
        base = declared.split("<", 1)[0].strip().rstrip("[]").rsplit(".", 1)[-1]
        return base == "ActorRef"

    def emit(node: Node, semantic: SemanticType, method: str | None) -> None:
        nonlocal found_any
        found_any = True
        start = node.start_point[0] + 1
        parent_id = find_enclosing_parent_id(start, record)
        record.statements.append(Statement(
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
                    if node_text(op, source) in ("!", "?") and is_actor_ref(left):
                        emit(c, "eventbus_send", "SEND")
            elif (
                c.type in ("function_definition", "function_declaration")
                and _is_receive_handler(c, source)
            ) or (c.type == "call_expression" and _is_receive_message_call(c, source)):
                emit(c, "eventbus_consumer", None)
            walk(c)

    walk(root)
    if found_any and not record.framework:
        record.framework = "akka"
