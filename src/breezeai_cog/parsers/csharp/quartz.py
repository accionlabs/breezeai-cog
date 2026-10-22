"""Additive detector: Quartz.NET scheduled jobs are ``timer`` bindings.

The record lives where a job and its schedule meet (``ScheduleJob(job, trigger)``,
``q.AddTrigger(t => t.ForJob(key)…)``, ``q.ScheduleJob<R>(…)``); the rules are shared with Java
Quartz in :mod:`..quartz_common`. This module reads the C# tree for them.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..index_common import record_distinct
from ..handler_sites import locate_method
from ..quartz_common import Call, detect_quartz
from ..treesitter import node_text


#: A trailing positional argument that is a cancellation token (``ScheduleJob(job, trigger, ct)``).
_TOKENS = frozenset({"cancellationtoken", "ct", "token", "stoppingtoken",
                     "cancellationtoken.none", "default", "default(cancellationtoken)"})

#: ``TimeSpan.From…`` → milliseconds per unit.
_TIMESPAN_UNITS = {"FromMilliseconds": 1, "FromSeconds": 1000, "FromMinutes": 60_000,
                   "FromHours": 3_600_000, "FromDays": 86_400_000}


class _CSharp:
    handler = "Execute"

    def __init__(self, source: bytes) -> None:
        self.source = source

    def call(self, node: Node) -> Call | None:
        if node.type != "invocation_expression":
            return None
        fn = node.child_by_field_name("function")
        receiver: Node | None = None
        if fn is not None and fn.type == "member_access_expression":
            receiver = fn.child_by_field_name("expression")
            fn = fn.child_by_field_name("name")
        if fn is None or fn.type not in ("identifier", "generic_name"):
            return None
        name = fn.named_children[0] if fn.type == "generic_name" else fn
        targs = next((c for c in fn.named_children if c.type == "type_argument_list"), None)
        type_arg = node_text(targs.named_children[0], self.source) \
            if targs is not None and len(targs.named_children) == 1 else None
        method = node_text(name, self.source).lower()
        return Call(node, receiver, method, type_arg, self._arguments(node, method))

    def _arguments(self, call: Node, method: str) -> list[Node]:
        """The call's argument values, without its cancellation token: every Quartz.NET scheduler
        call takes one (``ScheduleJob(job, trigger, cancellationToken: ct)``), and it is never a
        job or a trigger. A named ``cancellationToken:`` argument is always left out; a trailing
        positional one (``ct``, ``CancellationToken.None``, …) of a ``ScheduleJob`` call too."""
        arg_list = call.child_by_field_name("arguments")
        args: list[Node] = []
        for a in arg_list.named_children if arg_list is not None else []:
            if a.type != "argument" or not a.named_children:
                continue
            name = a.child_by_field_name("name")
            if name is not None and node_text(name, self.source).lower() == "cancellationtoken":
                continue
            args.append(a.named_children[-1])
        if method == "schedulejob" and args and \
                node_text(args[-1], self.source).replace(" ", "").lower() in _TOKENS:
            args.pop()
        return args

    def string(self, node: Node) -> str | None:
        if node.type == "string_literal":
            content = next((c for c in node.named_children if c.type == "string_literal_content"), None)
            return node_text(content, self.source) if content is not None else (
                "" if not node.named_children else None)
        if node.type == "verbatim_string_literal":
            return node_text(node, self.source)[2:-1]
        return None

    def integer(self, node: Node) -> int | None:
        text = node_text(node, self.source)
        return int(text) if node.type == "integer_literal" and text.isdigit() else None

    def class_ref(self, node: Node) -> str | None:
        if node.type != "typeof_expression" or not node.named_children:
            return None
        return node_text(node.named_children[0], self.source)

    def cast_value(self, node: Node) -> Node | None:
        if node.type == "cast_expression":
            return node.child_by_field_name("value")
        if node.type == "as_expression":
            return node.child_by_field_name("left")
        return None

    def duration(self, node: Node) -> int | None:
        """``TimeSpan.FromSeconds(40)`` → ``40000``: a ``TimeSpan.From…`` call of a whole number."""
        if node.type != "invocation_expression":
            return None
        fn = node.child_by_field_name("function")
        if fn is None or fn.type != "member_access_expression":
            return None
        receiver, name = fn.child_by_field_name("expression"), fn.child_by_field_name("name")
        if receiver is None or name is None \
                or node_text(receiver, self.source) not in ("TimeSpan", "System.TimeSpan"):
            return None
        unit = _TIMESPAN_UNITS.get(node_text(name, self.source))
        args = self._arguments(node, "")
        n = self.integer(args[0]) if len(args) == 1 else None
        return n * unit if unit is not None and n is not None else None

    def new_object(self, node: Node) -> tuple[str, list[Node]] | None:
        if node.type != "object_creation_expression":
            return None
        typ = node.child_by_field_name("type")
        arg_list = node.child_by_field_name("arguments")
        args = [a.named_children[-1] for a in arg_list.named_children
                if a.type == "argument" and a.named_children] if arg_list is not None else []
        return (node_text(typ, self.source), args) if typ is not None else None

    def variables(self, root: Node) -> dict[str, Node | None]:
        out: dict[str, Node | None] = {}
        stack = [root]
        while stack:
            n = stack.pop()
            if n.type == "variable_declarator" and len(n.named_children) > 1 \
                    and n.named_children[0].type == "identifier":
                record_distinct(out, node_text(n.named_children[0], self.source),
                                n.named_children[-1], same=lambda a, b: False)
            stack.extend(n.named_children)
        return out

    def returns(self, root: Node) -> dict[str, Node | None]:
        out: dict[str, Node | None] = {}
        stack = [root]
        while stack:
            n = stack.pop()
            stack.extend(n.named_children)
            params = n.child_by_field_name("parameters") if n.type == "method_declaration" else None
            name = n.child_by_field_name("name")
            if params is None or params.named_children or name is None:
                continue
            body = n.child_by_field_name("body")
            arrow = next((c for c in n.named_children if c.type == "arrow_expression_clause"), None)
            value: Node | None = None
            if arrow is not None and arrow.named_children:
                value = arrow.named_children[0]
            elif body is not None:
                ret = next((c for c in body.named_children if c.type == "return_statement"), None)
                value = ret.named_children[0] if ret is not None and ret.named_children else None
            if value is not None:
                record_distinct(out, node_text(name, self.source).lower(), value,
                                same=lambda a, b: False)
        return out


def _detect(dc: DetectContext) -> Detection | None:
    index = dc.ctx.resolution_index
    found = detect_quartz(dc.root, dc.source, dc.record, _CSharp(dc.source),
                          lambda job: locate_method(index, "csharp", job, _CSharp.handler))
    return Detection(found, label="quartz") if found else None


# An entry-point emitter, so fixture/test files are skipped.
register_detector(Detector(
    name="quartz",
    gate=Gate("csharp", order=60, guard=b"Quartz", skip_fixtures=True),
    detect=_detect, frameworks=("quartz",),
))
