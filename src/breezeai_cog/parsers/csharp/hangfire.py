"""Additive detector: Hangfire recurring jobs are ``timer`` bindings.

A recurring-job registration names one job method and its schedule, so it is a binding (contract
§4). The record lives at the registration call::

  RecurringJob.AddOrUpdate<Cleanup>("cleanup", c => c.Run(), "0 2 * * *");
      → timer  hangfire  PERIODIC  0 2 * * *   handler Run
  manager.AddOrUpdate("report", () => Reports.Send(), Cron.Daily());   // IRecurringJobManager
      → timer  hangfire  PERIODIC  0 0 * * *   handler Send

* **Receiver.** ``RecurringJob`` or a name declared in the file as ``IRecurringJobManager`` /
  ``RecurringJobManager``. ``AddOrUpdate`` alone is not enough: ``ConcurrentDictionary`` has one.
* **Handler.** The method the job lambda calls (``c => c.Run()`` → ``Run``), with its declaration
  line when it is declared once in this file. A lambda that is not a single call → no handler.
* **Schedule** (``endpoint``): the argument after the lambda — a string literal, or a no-argument
  ``Cron`` helper (``Cron.Daily()`` / ``Cron.Daily``), whose expression Hangfire fixes. Anything
  else (``Cron.Daily(2)``, a variable, a config value) → empty.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import node_text

#: ``Hangfire.Cron`` helpers called with no arguments → the expression they return.
_CRON_HELPERS = {
    "Minutely": "* * * * *",
    "Hourly": "0 * * * *",
    "Daily": "0 0 * * *",
    "Weekly": "0 0 * * 1",
    "Monthly": "0 0 1 * *",
    "Yearly": "0 0 1 1 *",
}

_MANAGER_DECL = re.compile(rb"\bI?RecurringJobManager\s+(\w+)")


def _walk(node: Node, typ: str) -> Iterator[Node]:
    if node.type == typ:
        yield node
    for c in node.named_children:
        yield from _walk(c, typ)


def _callee(call: Node, source: bytes) -> tuple[str, str] | None:
    """``Recv.Method<T>(…)`` → (receiver text, method name without type arguments)."""
    fn = call.child_by_field_name("function")
    if fn is None or fn.type != "member_access_expression":
        return None
    recv = fn.child_by_field_name("expression")
    name = fn.child_by_field_name("name")
    if recv is None or name is None:
        return None
    return node_text(recv, source), node_text(name, source).split("<", 1)[0]


def _job_method(lam: Node, source: bytes) -> str | None:
    """The method a job lambda calls: ``c => c.Run()`` / ``() => Reports.Send(x)`` → the name."""
    body = lam.child_by_field_name("body")
    if body is None or body.type != "invocation_expression":
        return None
    fn = body.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "member_access_expression":
        name = fn.child_by_field_name("name")
        return node_text(name, source).split("<", 1)[0] if name is not None else None
    return node_text(fn, source) if fn.type == "identifier" else None


def _schedule(arg: Node, source: bytes) -> str | None:
    if arg.type == "string_literal":
        text = node_text(arg, source)
        return text[1:-1] if text.startswith('"') and "{" not in text else None
    if arg.type == "verbatim_string_literal":
        return node_text(arg, source)[2:-1]
    target: Node | None = arg
    if arg.type == "invocation_expression":  # Cron.Daily() — only with no arguments
        args = arg.child_by_field_name("arguments")
        if args is None or args.named_children:
            return None
        target = arg.child_by_field_name("function")
    if target is None or target.type != "member_access_expression":
        return None
    recv = target.child_by_field_name("expression")
    name = target.child_by_field_name("name")
    if recv is None or name is None or node_text(recv, source) not in ("Cron", "Hangfire.Cron"):
        return None
    return _CRON_HELPERS.get(node_text(name, source))


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    receivers = {"RecurringJob", "Hangfire.RecurringJob"}
    receivers |= {m.decode() for m in _MANAGER_DECL.findall(source)}
    declared: dict[str, int | None] = {}
    for f in dc.record.functions:
        declared[f.name] = f.startLine if f.name not in declared else None
    found: list[Emit] = []
    for call in _walk(dc.root, "invocation_expression"):
        parts = _callee(call, source)
        if parts is None or parts[1] != "AddOrUpdate":
            continue
        if parts[0].removeprefix("this.") not in receivers:
            continue
        arg_list = call.child_by_field_name("arguments")
        args = [a.named_children[0] for a in arg_list.named_children
                if a.type == "argument" and a.named_children] if arg_list is not None else []
        lam_at = next((i for i, a in enumerate(args) if a.type == "lambda_expression"), None)
        if lam_at is None:
            continue
        handler = _job_method(args[lam_at], source)
        schedule = _schedule(args[lam_at + 1], source) if lam_at + 1 < len(args) else None
        found.append(Emit(
            call,
            {"semanticType": "timer", "framework": "hangfire", "method": "PERIODIC",
             "endpoint": schedule, "handler": handler,
             "handlerLine": declared.get(handler) if handler else None},
            enrich="unclassified",
        ))
    return Detection(found, label="hangfire") if found else None


# An entry-point emitter, so fixture/test files are skipped.
register_detector(Detector(
    name="hangfire",
    gate=Gate("csharp", order=50, guard=b"AddOrUpdate", skip_fixtures=True),
    detect=_detect, frameworks=("hangfire",),
))
