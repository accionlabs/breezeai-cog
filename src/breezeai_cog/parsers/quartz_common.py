"""Quartz scheduled jobs (Java Quartz and Quartz.NET): the rules shared by both detectors.

A Quartz job class only says *what* runs; a trigger says *when*. The binding (contract §3) is the
first construct that names both — one job and one schedule — and the ``timer`` record lives there::

    scheduler.scheduleJob(job, trigger);                       // job and trigger built before
    q.AddTrigger(t => t.ForJob(key).WithCronSchedule("…"));    // Quartz.NET DI: names the job
    TriggerBuilder.newTrigger().forJob(reportJob()).withSchedule(cronSchedule("…")).build()
    q.ScheduleJob<ReportJob>(t => t.WithCronSchedule("…"));    // Quartz.NET DI: both at once

The job class (``implements Job`` / ``: IJob``) has no address of its own, and building a job
(``newJob(…)``) or a trigger without ``forJob`` names only half: no record.

* **Handler.** ``execute`` / ``Execute`` of the job class, found through the job variable, a
  no-argument method returning the job, or the job key (``withIdentity`` ↔ ``forJob``) — within
  the file. Its line when the class is declared in the file. A job that cannot be traced → empty.
* **Schedule** (``endpoint``). A cron expression (``withCronSchedule`` / ``cronSchedule``), or a
  fixed interval in milliseconds (``withIntervalInSeconds(40)`` → ``40000``,
  ``repeatMinutelyForever(5)`` → ``300000``), as for Spring ``fixedRate``. ``method`` is
  ``PERIODIC``; a trigger with no schedule fires once (``TIMER``, endpoint empty). Daily-time and
  calendar intervals, a non-literal argument or two different schedules → ``PERIODIC``, empty.

Names are compared case-insensitively: Java writes ``forJob``, Quartz.NET ``ForJob``. Each
language supplies a :class:`Syntax` that reads its tree.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol

from tree_sitter import Node

from ..schemas import FileRecord
from .emits import Emit
from .index_common import record_distinct

_DEFAULT_GROUP = "DEFAULT"
_JOB_FACTORIES = frozenset({"newjob", "create", "addjob"})
_KEY_FACTORIES = frozenset({"jobkey", "create"})
_CRON = frozenset({"withcronschedule", "cronschedule", "cronschedulenonvalidatedexpression"})
#: Interval setters / shorthands → milliseconds per unit.
_INTERVALS = {
    "withintervalinmilliseconds": 1, "withintervalinseconds": 1000,
    "withintervalinminutes": 60_000, "withintervalinhours": 3_600_000,
    "repeatsecondlyforever": 1000, "repeatminutelyforever": 60_000,
    "repeathourlyforever": 3_600_000,
    "repeatsecondlyforevertotalcount": 1000, "repeatminutelyforevertotalcount": 60_000,
    "repeathourlyforevertotalcount": 3_600_000,
}
#: Calls only a trigger builder has: the chain they are on builds a trigger.
_TRIGGER_CALLS = frozenset({"forjob", "withschedule", "withsimpleschedule", "startnow", "startat",
                            "withcronschedule", "withdailytimeintervalschedule",
                            "withcalendarintervalschedule", "endat"})
#: Periodic schedules whose expression is not one value.
_OTHER_PERIODIC = frozenset({
    "withdailytimeintervalschedule", "dailytimeintervalschedule",
    "withcalendarintervalschedule", "calendarintervalschedule", "withinterval",
})


@dataclass(frozen=True)
class Call:
    """One method call: ``receiver.name<type_arg>(args)``."""

    node: Node
    receiver: Node | None
    name: str  # lower-cased
    type_arg: str | None
    args: list[Node]


class Syntax(Protocol):
    """How one language's tree spells what the rules read."""

    #: The handler method every job class implements: ``execute`` / ``Execute``.
    handler: str

    def call(self, node: Node) -> Call | None: ...
    def string(self, node: Node) -> str | None: ...
    def integer(self, node: Node) -> int | None: ...
    def class_ref(self, node: Node) -> str | None: ...  # ``R.class`` / ``typeof(R)``
    def new_object(self, node: Node) -> tuple[str, list[Node]] | None: ...  # ``new JobKey(…)``
    def variables(self, root: Node) -> dict[str, Node | None]: ...  # name → initializer
    #: no-argument method, lower-cased → the value it returns
    def returns(self, root: Node) -> dict[str, Node | None]: ...


@dataclass
class _File:
    syntax: Syntax
    root: Node
    source: bytes
    variables: dict[str, Node | None] = field(default_factory=dict)
    returns: dict[str, Node | None] = field(default_factory=dict)
    keys: dict[str, str | None] = field(default_factory=dict)  # job key → job class

    def calls(self, node: Node) -> Iterator[Call]:
        """Every call in ``node``'s subtree (lambda bodies and arguments included)."""
        stack = [node]
        while stack:
            n = stack.pop()
            c = self.syntax.call(n)
            if c is not None:
                yield c
            stack.extend(reversed(n.named_children))

    def chain(self, node: Node) -> list[Call]:
        """The calls of a builder chain, outermost first: ``a().b().c()`` → c, b, a."""
        out: list[Call] = []
        n: Node | None = node
        while n is not None:
            c = self.syntax.call(n)
            if c is None:
                break
            out.append(c)
            n = c.receiver
        return out

    def deref(self, node: Node, depth: int = 0) -> Node | None:
        """A variable or a no-argument method call → the value it stands for (else ``node``)."""
        if depth > 4:
            return None
        if node.type == "identifier":
            init = self.variables.get(self.text(node))
            return self.deref(init, depth + 1) if init is not None else None
        c = self.syntax.call(node)
        if c is not None and not c.args and c.type_arg is None and (
                c.receiver is None or self.text(c.receiver) == "this") and c.name in self.returns:
            value = self.returns[c.name]
            return self.deref(value, depth + 1) if value is not None else None
        return node

    def text(self, node: Node) -> str:
        return self.source[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def job_class(self, node: Node) -> str | None:
        """The job class a job expression builds: ``newJob(R.class)…`` / ``Create<R>()…``."""
        value = self.deref(node)
        if value is None:
            return None
        for c in self.chain(value):
            if c.name in _JOB_FACTORIES:
                ref = c.type_arg or (self.syntax.class_ref(c.args[0]) if c.args else None)
                if ref:
                    return ref.rsplit(".", 1)[-1]
        return None

    def key(self, args: list[Node]) -> str | None:
        """A job key from ``withIdentity`` / ``forJob`` arguments: ``("n")``, ``("n", "g")``,
        ``(jobKey("n"))``, ``(new JobKey("n"))``, or a variable holding one."""
        if not args:
            return None
        names = [self.syntax.string(a) for a in args[:2]]
        if all(n is not None for n in names):
            return f"{names[1] if len(names) > 1 else _DEFAULT_GROUP}.{names[0]}"
        if len(args) != 1:
            return None
        value = self.deref(args[0])
        if value is None:
            return None
        created = self.syntax.new_object(value)
        if created is not None and created[0].rsplit(".", 1)[-1] == "JobKey":
            return self.key(created[1])
        c = self.syntax.call(value)
        if c is not None and c.name in _KEY_FACTORIES and (
                c.receiver is None or self.text(c.receiver).endswith("JobKey")):
            return self.key(c.args)
        return None

    def is_trigger(self, node: Node) -> bool:
        """Whether ``node`` visibly builds a trigger: its chain starts a trigger builder
        (``newTrigger()`` / ``TriggerBuilder.Create()``) or configures one. A value from a
        call we cannot follow (``jobContext.getTrigger()``) is not one: its schedule is unknown,
        not absent."""
        for c in self.chain(node):
            if c.name == "newtrigger" or c.name in _TRIGGER_CALLS or (
                    c.name == "create" and c.receiver is not None
                    and self.text(c.receiver).endswith("TriggerBuilder")):
                return True
        return False

    def schedule(self, node: Node) -> tuple[str | None, str | None]:
        """``(method, endpoint)`` of the trigger built in ``node``."""
        found: set[tuple[str, str | None]] = set()
        for c in self.calls(node):
            if c.name in _CRON:
                found.add(("PERIODIC", self.syntax.string(c.args[0]) if c.args else None))
            elif c.name in _INTERVALS:
                n = self.syntax.integer(c.args[0]) if c.args else (
                    1 if c.name.startswith("repeat") else None)
                found.add(("PERIODIC", str(n * _INTERVALS[c.name]) if n is not None else None))
            elif c.name in _OTHER_PERIODIC:
                found.add(("PERIODIC", None))
        if not found:
            return "TIMER", None
        return found.pop() if len(found) == 1 else ("PERIODIC", None)


def _register_keys(f: _File) -> None:
    """Job key → job class, from every job built in the file: ``newJob(R.class).withIdentity(k)``,
    ``Create<R>().WithIdentity(k)``, ``q.AddJob<R>(k)`` / ``q.AddJob<R>(o => o.WithIdentity(k))``."""
    for c in f.calls(f.root):
        if c.name != "withidentity":
            continue
        job = f.job_class(c.node)  # newJob(R.class).withIdentity(k) — the factory is in the chain
        if job is None:  # Quartz.NET: q.AddJob<R>(o => o.WithIdentity(k)) — the factory encloses
            n = c.node.parent
            while n is not None and job is None:
                enclosing = f.syntax.call(n)
                if enclosing is not None and enclosing.name == "addjob":
                    job = enclosing.type_arg
                n = n.parent
        key = f.key(c.args)
        if job is not None and key is not None:
            record_distinct(f.keys, key, job)
    for c in f.calls(f.root):
        if c.name == "addjob" and c.type_arg and c.args:
            key = f.key(c.args[:1])
            if key is not None:
                record_distinct(f.keys, key, c.type_arg)


def _top(f: _File, node: Node) -> Node:
    """The outermost call of the builder chain ``node`` is part of."""
    top, n = node, node.parent
    while n is not None:
        if n.type == "member_access_expression":  # C# wraps ``receiver.Name`` around the receiver
            n = n.parent
            continue
        c = f.syntax.call(n)
        if c is None or c.receiver is None or not (
                c.receiver.start_byte <= top.start_byte and top.end_byte <= c.receiver.end_byte):
            break
        top, n = n, n.parent
    return top


def detect_quartz(root: Node, source: bytes, record: FileRecord, syntax: Syntax) -> list[Emit]:
    """The Quartz ``timer`` bindings of one file (see the module docstring)."""
    f = _File(syntax, root, source, syntax.variables(root), syntax.returns(root))
    _register_keys(f)
    handler_lines = _handler_lines(record, syntax.handler)

    def emit(node: Node, job: str | None, trigger: Node | None) -> Emit:
        # A trigger that cannot be traced: neither its kind nor its schedule is known.
        method, endpoint = f.schedule(trigger) if trigger is not None else (None, None)
        return Emit(node, {
            "semanticType": "timer", "framework": "quartz", "method": method,
            "endpoint": endpoint, "handler": syntax.handler if job else None,
            "handlerLine": handler_lines.get(job) if job else None,
        }, enrich="unclassified")

    found: list[Emit] = []
    done: set[int] = set()  # trigger chains recorded at their scheduleJob call
    for c in f.calls(root):
        if c.name != "schedulejob":
            continue
        if c.type_arg and c.args:  # Quartz.NET: q.ScheduleJob<R>(t => t.WithCronSchedule(…))
            found.append(emit(c.node, c.type_arg, c.args[0]))
        elif len(c.args) == 2:  # scheduler.scheduleJob(job, trigger)
            trigger = f.deref(c.args[1])
            trigger = trigger if trigger is not None and f.is_trigger(trigger) else None
            if trigger is not None:
                done.add(_top(f, trigger).start_byte)
            found.append(emit(c.node, f.job_class(c.args[0]), trigger))
    for c in f.calls(root):
        if c.name != "forjob":
            continue
        top = _top(f, c.node)
        if top.start_byte in done:
            continue
        done.add(top.start_byte)
        job = f.job_class(c.args[0]) if len(c.args) == 1 else None
        if job is None:
            key = f.key(c.args)
            job = f.keys.get(key) if key is not None else None
        found.append(emit(top, job, top))
    found.sort(key=lambda e: (e.node.start_byte, -e.node.end_byte))
    return found


def _handler_lines(record: FileRecord, handler: str) -> dict[str, int | None]:
    """Job class declared in this file → the line of its handler method (one class per name)."""
    class_ids = {c.id: c.name for c in record.classes}
    out: dict[str, int | None] = {}
    for fn in record.functions:
        cls = class_ids.get(fn.parentId or "")
        if fn.name == handler and cls is not None:
            record_distinct(out, cls, fn.startLine)
    return out
