"""Conservative Go event-bus and timer statement detection."""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import Statement
from ..treesitter import node_text


def _call_parts(call: Node, source: bytes) -> tuple[str, str] | None:
    function = call.child_by_field_name("function")
    if function is None or function.type != "selector_expression":
        return None
    operand = function.child_by_field_name("operand")
    field = function.child_by_field_name("field")
    if operand is None or field is None:
        return None
    return node_text(operand, source), node_text(field, source)


def _first_string(call: Node, source: bytes) -> str | None:
    arguments = call.child_by_field_name("arguments")
    if arguments is None or not arguments.named_children:
        return None
    first = arguments.named_children[0]
    if first.type in {"interpreted_string_literal", "raw_string_literal"}:
        return node_text(first, source).strip('"`')
    return None


def _receiver_types(root: Node, source: bytes) -> dict[str, str]:
    types: dict[str, str] = {}

    def walk(node: Node) -> None:
        if node.type == "parameter_declaration":
            name = node.child_by_field_name("name")
            type_node = node.child_by_field_name("type")
            if name is not None and type_node is not None:
                types[node_text(name, source)] = node_text(type_node, source)
        for child in node.named_children:
            walk(child)

    walk(root)
    return types


def _is_package_receiver(receiver: str, receiver_types: dict[str, str], package: str) -> bool:
    receiver_name = receiver.rsplit(".", 1)[-1].lower()
    if receiver_name == package:
        return True
    type_text = receiver_types.get(receiver, "").lstrip("*").split("[", 1)[0]
    return type_text.rsplit(".", 1)[-2].lower() == package if "." in type_text else False


def _classify(
    receiver: str,
    method: str,
    source_text: str,
    receiver_types: dict[str, str],
) -> tuple[str, str] | None:
    lower_source = source_text.lower()
    receiver_name = receiver.rsplit(".", 1)[-1].lower()
    if (
        "nats-io" in lower_source
        and _is_package_receiver(receiver, receiver_types, "nats")
        and method in {"Publish", "Request"}
    ):
        return "eventbus_send", "nats"
    if (
        "nats-io" in lower_source
        and _is_package_receiver(receiver, receiver_types, "nats")
        and method in {"Subscribe", "QueueSubscribe"}
    ):
        return "eventbus_consumer", "nats"
    if (
        "kafka" in lower_source
        and _is_package_receiver(receiver, receiver_types, "kafka")
        and method in {"SendMessage", "WriteMessages"}
    ):
        return "eventbus_send", "kafka"
    if (
        "kafka" in lower_source
        and _is_package_receiver(receiver, receiver_types, "kafka")
        and method in {"ReadMessage", "FetchMessage"}
    ):
        return "eventbus_consumer", "kafka"
    if receiver_name == "time" and method in {"NewTicker", "AfterFunc", "Tick"}:
        return "timer", "time"
    return None


def detect_events(
    root: Node,
    source: bytes,
    path: str,
    *,
    seen_ids: set[str],
    parent_id: str,
    owners: list[tuple[int, int, str]] | None = None,
) -> list[Statement]:
    source_text = source.decode("utf-8", "replace")
    receiver_types = _receiver_types(root, source)
    out: list[Statement] = []

    def walk(node: Node) -> None:
        if node.type == "call_expression":
            parts = _call_parts(node, source)
            if parts is not None:
                receiver, method = parts
                classified = _classify(receiver, method, source_text, receiver_types)
                if classified is not None:
                    semantic, framework = classified
                    line = node.start_point[0] + 1
                    owner_id = parent_id
                    for start, end, candidate_id in owners or []:
                        if start <= line <= end:
                            owner_id = candidate_id
                            break
                    out.append(Statement(
                        id=disambiguate(statement_id(path, line, node.start_point[1]), seen_ids),
                        parentId=owner_id,
                        nodeType=node.type,
                        semanticType=semantic,
                        text=node_text(node, source),
                        method=method,
                        endpoint=_first_string(node, source),
                        framework=framework,
                        startLine=line,
                        endLine=node.end_point[0] + 1,
                        path=path,
                    ))
        for child in node.named_children:
            walk(child)

    walk(root)
    return out