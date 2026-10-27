"""Scheduled jobs in Go: robfig/cron and the ``time`` package's tickers::

    c := cron.New()
    c.AddFunc("0 2 * * *", nightly)        → timer PERIODIC "0 2 * * *", handler nightly
    c.AddJob("@hourly", job)               → timer PERIODIC "@hourly", handler job
    t := time.NewTicker(30 * time.Second)  → timer PERIODIC "30000" (ms), handler = the enclosing function
    time.AfterFunc(5*time.Minute, f)       → timer TIMER "300000", handler f

Intervals are written in milliseconds, as for Quartz and Vert.x; one that is not a constant
expression leaves ``endpoint`` empty. ``time.After`` / ``time.NewTimer`` are timeouts in the
code's own flow, not schedules — not recorded."""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..go.imports import GoIndex
from ..go.routing import file_analyzer, string_value
from ..go.values import ValueSpec, call_type, handler_fill, typed_calls
from ..treesitter import node_text

#: robfig/cron, any major version (``…/cron/v3`` is matched by the spec's version rule).
CRON = [ValueSpec(import_path="github.com/robfig/cron", types=frozenset({"Cron"}), constructors={"New": "Cron"})]
TIME = ValueSpec(import_path="time", types=frozenset())

_UNITS_MS = {"Nanosecond": 1e-6, "Microsecond": 1e-3, "Millisecond": 1, "Second": 1000,
             "Minute": 60_000, "Hour": 3_600_000}


def _millis(node: Node | None, a) -> float | None:
    """``30 * time.Second`` / ``time.Minute`` / ``2 * time.Hour + 30*time.Minute`` → ms."""
    if node is None:
        return None
    if node.type == "parenthesized_expression" and node.named_children:
        return _millis(node.named_children[0], a)
    if node.type == "int_literal":
        return float(node_text(node, a.source).replace("_", ""))
    if node.type == "selector_expression":
        operand = node.child_by_field_name("operand")
        unit = node_text(node.child_by_field_name("field"), a.source)
        if operand is not None and a.aliases.get(node_text(operand, a.source)) == "time":
            return _UNITS_MS.get(unit)
        return None
    if node.type == "binary_expression":
        op = node_text(node.child_by_field_name("operator"), a.source)
        left, right = _millis(node.child_by_field_name("left"), a), _millis(node.child_by_field_name("right"), a)
        if left is None or right is None:
            return None
        return left * right if op == "*" else left + right if op == "+" else None
    return None


def _interval(node: Node | None, a) -> str | None:
    ms = _millis(node, a)
    return str(int(ms)) if ms is not None and ms == int(ms) else None


#: The detector's own byte guard (two libraries, so no single ``Gate.guard``).
_MARKERS = (b"robfig/cron", b"NewTicker", b"AfterFunc", b"time.Tick(")


def detect_scheduling(dc: DetectContext) -> Detection | None:
    if not any(m in dc.source for m in _MARKERS):
        return None
    index = dc.ctx.resolution_index if isinstance(dc.ctx.resolution_index, GoIndex) else None
    a = file_analyzer(dc.root, dc.source, dc.path, index)
    found: list[Emit] = []
    for spec in CRON:
        for tc in typed_calls(a, spec):
            if tc.method in {"AddFunc", "AddJob"} and len(tc.args) >= 2:
                found.append(Emit(tc.call, {"semanticType": "timer", "framework": "cron", "method": "PERIODIC",
                                            "endpoint": string_value(tc.args[0], a.source, a.consts)},
                                  fill=handler_fill(a, tc.call, tc.args[1], tc.function), enrich="any"))
    if "time" in a.aliases.values():
        stack = [(dc.root, None)]
        while stack:
            node, function = stack.pop()
            if node.type in {"function_declaration", "method_declaration"}:
                function = node
            stack.extend((c, function) for c in reversed(node.named_children))
            name = call_type(a, TIME, node) if node.type == "call_expression" else None
            if name not in {"NewTicker", "Tick", "AfterFunc"}:
                continue
            args = node.child_by_field_name("arguments")
            args = [c for c in args.named_children if c.type != "comment"] if args is not None else []
            if name == "AfterFunc":
                fill = handler_fill(a, node, args[1] if len(args) > 1 else None, function)
                method = "TIMER"
            else:
                fill = handler_fill(a, node, None, function)
                method = "PERIODIC"
            found.append(Emit(node, {"semanticType": "timer", "framework": "time", "method": method,
                                     "endpoint": _interval(args[0] if args else None, a)},
                              fill=fill, enrich="any"))
    return Detection(found) if found else None


register_detector(Detector(
    name="go-scheduling", gate=Gate("go", order=100, skip_fixtures=True),
    detect=detect_scheduling, frameworks=("cron", "time"),
))
