"""WordPress hook registrations as additive event consumers."""

from __future__ import annotations

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..php_route_common import callback_handler, string_value, walk
from ..treesitter import node_text


def detect_wordpress_hooks(dc: DetectContext) -> Detection | None:
    source = dc.source
    if b"add_action" not in source and b"add_filter" not in source:
        return None
    found: list[Emit] = []
    for node in walk(dc.root):
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
        # An unresolved hook name is still a binding: its endpoint stays empty (contract §8).
        hook = string_value(values[0], source)
        handler, handler_line = callback_handler(values[1], source)
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
