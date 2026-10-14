"""Additive detector: Quartz scheduled jobs are ``timer`` bindings.

The record lives where a job and its schedule meet (``scheduler.scheduleJob(job, trigger)``, a
trigger built with ``forJob(…)`` — a Spring ``@Bean`` trigger included); the rules are shared
with Quartz.NET in :mod:`..quartz_common`. This module reads the Java tree for them.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..index_common import record_distinct
from ..quartz_common import Call, detect_quartz
from ..treesitter import node_text


class _Java:
    handler = "execute"

    def __init__(self, source: bytes) -> None:
        self.source = source

    def _args(self, node: Node) -> list[Node]:
        arg_list = node.child_by_field_name("arguments")
        return list(arg_list.named_children) if arg_list is not None else []

    def call(self, node: Node) -> Call | None:
        if node.type != "method_invocation":
            return None
        name = node.child_by_field_name("name")
        if name is None:
            return None
        return Call(node, node.child_by_field_name("object"),
                    node_text(name, self.source).lower(), None, self._args(node))

    def string(self, node: Node) -> str | None:
        if node.type != "string_literal":
            return None
        frags = [c for c in node.named_children if c.type == "string_fragment"]
        if len(frags) != len(node.named_children):  # an escape or interpolation: not plain
            return None
        return "".join(node_text(c, self.source) for c in frags)

    def integer(self, node: Node) -> int | None:
        if node.type != "decimal_integer_literal":
            return None
        text = node_text(node, self.source).replace("_", "").rstrip("lL")
        return int(text) if text.isdigit() else None

    def class_ref(self, node: Node) -> str | None:
        if node.type != "class_literal" or not node.named_children:
            return None
        return node_text(node.named_children[0], self.source)

    def cast_value(self, node: Node) -> Node | None:
        return node.child_by_field_name("value") if node.type == "cast_expression" else None

    def duration(self, node: Node) -> int | None:
        return None  # Java Quartz takes intervals as numbers (``withIntervalInSeconds``)

    def new_object(self, node: Node) -> tuple[str, list[Node]] | None:
        if node.type != "object_creation_expression":
            return None
        typ = node.child_by_field_name("type")
        return (node_text(typ, self.source), self._args(node)) if typ is not None else None

    def variables(self, root: Node) -> dict[str, Node | None]:
        out: dict[str, Node | None] = {}
        stack = [root]
        while stack:
            n = stack.pop()
            if n.type == "variable_declarator":
                name, value = n.child_by_field_name("name"), n.child_by_field_name("value")
                if name is not None and value is not None:
                    record_distinct(out, node_text(name, self.source), value,
                                    same=lambda a, b: False)
            stack.extend(n.named_children)
        return out

    def returns(self, root: Node) -> dict[str, Node | None]:
        out: dict[str, Node | None] = {}
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            params = n.child_by_field_name("parameters") if n.type == "method_declaration" else None
            name, body = n.child_by_field_name("name"), n.child_by_field_name("body")
            if params is None or params.named_children or name is None or body is None:
                continue
            ret = next((c for c in body.named_children if c.type == "return_statement"), None)
            if ret is not None and ret.named_children:
                record_distinct(out, node_text(name, self.source).lower(), ret.named_children[0],
                                same=lambda a, b: False)
        return out


def _detect(dc: DetectContext) -> Detection | None:
    found = detect_quartz(dc.root, dc.source, dc.record, _Java(dc.source))
    return Detection(found, label="quartz") if found else None


# An entry-point emitter, so fixture/test files are skipped.
register_detector(Detector(
    name="quartz",
    gate=Gate("java", order=10, guard=b"quartz", skip_fixtures=True),
    detect=_detect, frameworks=("quartz",),
))
