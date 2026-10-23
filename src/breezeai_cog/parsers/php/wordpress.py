"""WordPress hook registrations as additive event consumers."""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import node_text


def _walk(node: Node) -> Iterator[Node]:
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _value(node: Node, source: bytes) -> str | None:
    if node.type != "string":
        return None
    raw = node_text(node, source)
    if len(raw) < 2 or raw[0] not in {"'", '"'} or raw[-1] != raw[0]:
        return None
    if any(child.type != "string_content" for child in node.named_children):
        return None
    return "".join(node_text(child, source) for child in node.named_children)


def _callable(node: Node, source: bytes) -> tuple[str | None, int | None]:
    if node.type == "argument" and len(node.named_children) == 1:
        node = node.named_children[0]
    value = _value(node, source)
    if value is not None:
        if "::" in value:
            owner, method = value.rsplit("::", 1)
            return f"{owner}@{method}", node.start_point[0] + 1
        if "@" in value:
            return value.replace("@", "@", 1), node.start_point[0] + 1
        return value, node.start_point[0] + 1
    if node.type != "array_creation_expression":
        return None, None
    elements = [_value(c.named_children[0], source) if c.named_children else None
                for c in node.named_children]
    if len(elements) < 2 or elements[1] is None:
        return None, None
    target = node.named_children[0].named_children[0] if node.named_children else None
    class_name: str | None = None
    if target is not None and target.type == "class_constant_access_expression":
        class_name = node_text(target, source).rsplit("::", 1)[0]
    method = elements[1]
    return (f"{class_name}@{method}" if class_name else method), node.start_point[0] + 1


def detect_wordpress_hooks(dc: DetectContext) -> Detection | None:
    source = dc.source
    if b"add_action" not in source and b"add_filter" not in source:
        return None
    found: list[Emit] = []
    for node in _walk(dc.root):
        if node.type != "function_call_expression":
            continue
        function = node.child_by_field_name("function")
        if function is None or node_text(function, source).rsplit("\\", 1)[-1] not in {
            "add_action", "add_filter",
        }:
            continue
        args = node.child_by_field_name("arguments")
        values = list(args.named_children) if args is not None else []
        if len(values) < 2:
            continue
        hook = _value(values[0].named_children[0], source) if values[0].named_children else None
        if hook is None:
            continue
        handler, handler_line = _callable(values[1], source)
        found.append(Emit(
            node,
            {
                "semanticType": "eventbus_consumer",
                "method": "CONSUMER",
                "endpoint": hook,
                "handler": handler,
                "handlerLine": handler_line,
                "framework": "wordpress",
            },
            enrich="unclassified",
        ))
    return Detection(found, label="wordpress") if found else None


register_detector(
    Detector(
        name="wordpress-hooks",
        gate=Gate("php", order=10, skip_fixtures=True),
        detect=detect_wordpress_hooks,
        frameworks=("wordpress",),
    )
)
