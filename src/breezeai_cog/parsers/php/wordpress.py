"""WordPress hook registrations as additive event consumers."""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..php_route_common import string_value, unwrap, walk
from ..treesitter import node_text


_CLASS_SCOPES = {"class_declaration", "trait_declaration", "enum_declaration"}
_SELF = {"$this", "self::class", "static::class", "__CLASS__"}


def _enclosing_class(node: Node, source: bytes) -> str | None:
    parent = node.parent
    while parent is not None:
        if parent.type in _CLASS_SCOPES:
            name = parent.child_by_field_name("name")
            return node_text(name, source) if name is not None else None
        parent = parent.parent
    return None


def _callable(node: Node, source: bytes) -> tuple[str | None, int | None]:
    """(handler as written, line of an inline closure). The handler of a named callback is
    located after parsing; a callback on an object whose class the syntax doesn't show
    (``[$service, 'run']``) names no handler — a bare ``run`` would be read as a global
    function."""
    node = unwrap(node) or node
    if node.type in {"anonymous_function", "arrow_function"}:
        return None, node.start_point[0] + 1
    value = string_value(node, source)
    if value is not None:
        cls, sep, name = value.lstrip("\\").rpartition("::")
        return (f"{cls}@{name}" if sep else value.lstrip("\\")), None
    if node.type != "array_creation_expression" or len(node.named_children) != 2:
        return None, None
    target, member = (unwrap(next(iter(e.named_children), None)) for e in node.named_children)
    method = string_value(member, source)
    if target is None or method is None:
        return None, None
    written = node_text(target, source)
    owner: str | None
    if written in _SELF:
        owner = _enclosing_class(node, source)
    elif target.type == "class_constant_access_expression" and written.lower().endswith("::class"):
        owner = written.rsplit("::", 1)[0].lstrip("\\")
    else:
        owner = (string_value(target, source) or "").lstrip("\\")
    return (f"{owner}@{method}" if owner else None), None


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
