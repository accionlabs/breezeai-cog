"""Additive detector: Hangfire recurring jobs are ``timer`` bindings.

A recurring-job registration names one job method and its schedule, so it is a binding (contract
§4). The record lives at the registration call::

  RecurringJob.AddOrUpdate<Cleanup>("cleanup", c => c.Run(), "0 2 * * *");
      → timer  hangfire  PERIODIC  0 2 * * *   handler Run
  manager.AddOrUpdate("report", () => Reports.Send(), Cron.Daily());   // IRecurringJobManager
      → timer  hangfire  PERIODIC  0 0 * * *   handler Send

* **Receiver.** ``RecurringJob`` or a name declared in the file as ``IRecurringJobManager`` /
  ``RecurringJobManager``. ``AddOrUpdate`` alone is not enough: ``ConcurrentDictionary`` has one.
* **Handler.** The method the job lambda calls (``c => c.Run()`` → ``Run``). A lambda that is not
  a single call → no handler. Where it is declared (``handlerLine`` / ``handlerPath``,
  :func:`..handler_sites.locate_method`) needs its class: the type argument for a call on the
  lambda's parameter (``AddOrUpdate<Cleanup>(…, c => c.Run())``), the receiver when it binds to a
  type (``() => Reports.Send()``), the class the registration is written in for a bare call
  (``() => Send()``). A variable receiver, an inherited method, two declarations → no line.
* **Schedule** (``endpoint``): the argument after the lambda — a string literal, or a ``Cron``
  helper whose arguments are literals (``Cron.Daily`` / ``Cron.Daily()`` → ``0 0 * * *``,
  ``Cron.Daily(2, 30)`` → ``30 2 * * *``, ``Cron.Weekly(DayOfWeek.Friday, hour: 3)`` →
  ``0 3 * * 5``): Hangfire's own formulas. Anything else (a variable, a config value) → empty.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..handler_sites import handler_location, locate_method
from ..treesitter import node_text
from .imports import CSharpIndex, in_scope_namespaces, type_files

#: ``Hangfire.Cron`` helpers → (parameters with their defaults, the expression). As in
#: ``Hangfire.Core/Cron.cs``: ``Daily(int hour, int minute)`` → ``$"{minute} {hour} * * *"``,
#: ``Daily()`` = ``Daily(hour: 0)`` = ``Daily(0, minute: 0)``. ``None``: a required parameter.
_CRON_HELPERS: dict[str, tuple[tuple[tuple[str, int | None], ...], str]] = {
    "Minutely": ((), "* * * * *"),
    "Hourly": ((("minute", 0),), "{minute} * * * *"),
    "Daily": ((("hour", 0), ("minute", 0)), "{minute} {hour} * * *"),
    "Weekly": ((("dayOfWeek", 1), ("hour", 0), ("minute", 0)), "{minute} {hour} * * {dayOfWeek}"),
    "Monthly": ((("day", 1), ("hour", 0), ("minute", 0)), "{minute} {hour} {day} * *"),
    "Yearly": ((("month", 1), ("day", 1), ("hour", 0), ("minute", 0)),
               "{minute} {hour} {day} {month} *"),
    "Never": ((), "0 0 31 2 *"),
    "MinuteInterval": ((("interval", None),), "*/{interval} * * * *"),
    "HourInterval": ((("interval", None),), "0 */{interval} * * *"),
    "DayInterval": ((("interval", None),), "0 0 */{interval} * *"),
    "MonthInterval": ((("interval", None),), "0 0 1 */{interval} *"),
}
#: ``System.DayOfWeek`` members → their value (``(int) dayOfWeek`` in ``Cron.Weekly``).
_DAYS = {d: i for i, d in enumerate(
    ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"))}

_CLASSES = ("class_declaration", "struct_declaration", "record_declaration")
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


def _job_call(lam: Node, source: bytes) -> tuple[str | None, str] | None:
    """The method a job lambda calls, with its receiver: ``c => c.Run()`` → ("c", "Run"),
    ``() => Reports.Send(x)`` → ("Reports", "Send"), ``() => Send()`` → (None, "Send")."""
    body = lam.child_by_field_name("body")
    if body is None or body.type != "invocation_expression":
        return None
    fn = body.child_by_field_name("function")
    if fn is None:
        return None
    if fn.type == "member_access_expression":
        recv = fn.child_by_field_name("expression")
        name = fn.child_by_field_name("name")
        if recv is None or name is None:
            return None
        return node_text(recv, source), node_text(name, source).split("<", 1)[0]
    return (None, node_text(fn, source)) if fn.type == "identifier" else None


def _lambda_params(lam: Node, source: bytes) -> set[str]:
    params = lam.child_by_field_name("parameters")
    if params is None:
        return set()
    if params.type == "implicit_parameter":  # ``c => …``
        return {node_text(params, source)}
    return {node_text(p.child_by_field_name("name") or p, source) for p in params.named_children
            if p.type == "parameter"}


def _type_argument(call: Node, source: bytes) -> str | None:
    """``RecurringJob.AddOrUpdate<Cleanup>(…)`` → ``Cleanup``; None without exactly one."""
    fn = call.child_by_field_name("function")
    name = fn.child_by_field_name("name") if fn is not None else None
    targs = next((c for c in name.named_children if c.type == "type_argument_list"), None) \
        if name is not None else None
    if targs is None or len(targs.named_children) != 1:
        return None
    return node_text(targs.named_children[0], source)


def _enclosing_class(node: Node, source: bytes) -> str | None:
    cur = node.parent
    while cur is not None and cur.type not in _CLASSES:
        cur = cur.parent
    nm = cur.child_by_field_name("name") if cur is not None else None
    return node_text(nm, source) if nm is not None else None


def _int(arg: Node, source: bytes) -> int | None:
    """An ``int`` literal or a ``DayOfWeek`` member; anything else → None."""
    if arg.type == "integer_literal" and node_text(arg, source).isdigit():
        return int(node_text(arg, source))
    if arg.type == "member_access_expression":
        recv = arg.child_by_field_name("expression")
        name = arg.child_by_field_name("name")
        if recv is not None and name is not None and \
                node_text(recv, source) in ("DayOfWeek", "System.DayOfWeek"):
            return _DAYS.get(node_text(name, source))
    return None


def _cron_helper(helper: str, args: Node | None, source: bytes) -> str | None:
    """``Cron.<helper>(args)`` → the expression Hangfire returns; None when an argument is not a
    literal, or does not fit the helper's parameters."""
    spec = _CRON_HELPERS.get(helper)
    if spec is None:
        return None
    params, template = spec
    values: dict[str, int | None] = dict(params)
    names = [p for p, _ in params]
    for i, arg in enumerate(args.named_children if args is not None else []):
        named = arg.child_by_field_name("name")
        key = node_text(named, source) if named is not None else (names[i] if i < len(names) else None)
        expr = arg.named_children[-1] if arg.named_children else None
        value = _int(expr, source) if expr is not None else None
        if key not in values or value is None:
            return None
        values[key] = value
    if any(v is None for v in values.values()):
        return None
    return template.format(**values)


def _schedule(arg: Node, source: bytes) -> str | None:
    if arg.type == "string_literal":
        text = node_text(arg, source)
        return text[1:-1] if text.startswith('"') and "{" not in text else None
    if arg.type == "verbatim_string_literal":
        return node_text(arg, source)[2:-1]
    target: Node | None = arg
    args: Node | None = None
    if arg.type == "invocation_expression":  # Cron.Daily(2) — Cron.Daily is the method group
        args = arg.child_by_field_name("arguments")
        target = arg.child_by_field_name("function")
    if target is None or target.type != "member_access_expression":
        return None
    recv = target.child_by_field_name("expression")
    name = target.child_by_field_name("name")
    if recv is None or name is None or node_text(recv, source) not in ("Cron", "Hangfire.Cron"):
        return None
    return _cron_helper(node_text(name, source), args, source)


def _detect(dc: DetectContext) -> Detection | None:
    source = dc.source
    receivers = {"RecurringJob", "Hangfire.RecurringJob"}
    receivers |= {m.decode() for m in _MANAGER_DECL.findall(source)}
    index = dc.ctx.resolution_index
    own_classes = {c.name for c in dc.record.classes}
    scopes: set[str] | None = None

    def files_of(cls: str) -> set[str]:
        """The files ``cls`` binds to by C# rules (as the Quartz detector binds a job class)."""
        nonlocal scopes
        if cls in own_classes:
            return {dc.path}
        if not isinstance(index, CSharpIndex):
            return set()
        if scopes is None:
            scopes = in_scope_namespaces(dc.root, source) | index.global_usings
        return type_files(cls, scopes, index, dc.path)

    def locate(call: Node, lam: Node) -> tuple[str | None, tuple[str, int] | None]:
        """(handler, its declaration site) — see the module docstring."""
        target = _job_call(lam, source)
        if target is None:
            return None, None
        recv, method = target
        if recv is None:  # a bare call: the registering class's own method
            cls = _enclosing_class(call, source)
            return method, locate_method(index, "csharp", cls, method, within={dc.path})
        if recv in _lambda_params(lam, source):
            # the type argument: a type, so its simple name alone when it binds to no file
            cls = _type_argument(call, source)
            files = files_of(cls) if cls else set()
            return method, locate_method(index, "csharp", cls, method, within=files or None)
        files = files_of(recv)  # a static call — only when the receiver binds to a type
        return method, locate_method(index, "csharp", recv, method, within=files) if files else None
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
        handler, site = locate(call, args[lam_at])
        line, path = handler_location(site, dc.path)
        schedule = _schedule(args[lam_at + 1], source) if lam_at + 1 < len(args) else None
        found.append(Emit(
            call,
            {"semanticType": "timer", "framework": "hangfire", "method": "PERIODIC",
             "endpoint": schedule, "handler": handler,
             "handlerLine": line, "handlerPath": path},
            enrich="unclassified",
        ))
    return Detection(found, label="hangfire") if found else None


# An entry-point emitter, so fixture/test files are skipped.
register_detector(Detector(
    name="hangfire",
    gate=Gate("csharp", order=50, guard=b"AddOrUpdate", skip_fixtures=True),
    detect=_detect, frameworks=("hangfire",),
))
