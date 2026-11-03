"""Scheduled Apex and Platform Event publishes: fields that need more than one call node.

``classify_node`` marks ``System.schedule(name, cron, job)`` as a ``timer`` (cron as
``endpoint``) and ``EventBus.publish`` as an ``eventbus_publish``. This pass, run on the
finished record, adds what the call alone does not give: the framework label, and for a
timer its handler — ``execute`` of the job's class, located through ``handler_sites``
(``apex.method-lines``) or, without an index, in this file. Shaped like Quartz's
``scheduleJob(job, trigger)``: the call names one job and its schedule.
"""

from __future__ import annotations

from typing import Any

from tree_sitter import Node

from ...schemas import FileRecord, Statement
from ..handler_sites import handler_location, locate_method
from .detect import in_test_code, is_schedule_call, schedule_job_class

SCHEDULE_FRAMEWORK = "apex-schedule"
EVENT_FRAMEWORK = "apex-platform-event"


def _schedule_calls(root: Node, source: bytes) -> list[Node]:
    found: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if is_schedule_call(node, source) and not in_test_code(node, source):
            found.append(node)
        stack.extend(node.named_children)
    return sorted(found, key=lambda n: n.start_byte)


def _execute_here(record: FileRecord, cls: str) -> int | None:
    """Line of ``cls.execute`` declared in this file, when the class is here."""
    owner = next((c for c in record.classes if c.name.lower() == cls.lower()), None)
    if owner is None:
        return None
    fn = next((f for f in record.functions
               if f.parentId == owner.id and f.name.lower() == "execute"), None)
    return fn.startLine if fn is not None else None


def _locate(index: Any | None, record: FileRecord, cls: str | None) -> tuple[int | None, str | None]:
    if not cls:
        return None, None
    here = _execute_here(record, cls)
    if here is not None:
        return here, None
    return handler_location(locate_method(index, "apex", cls, "execute"), record.path)


def _demote(s: Statement) -> None:
    s.semanticType = None
    s.method = s.endpoint = None


def enrich_jobs(record: FileRecord, root: Node, source: bytes, index: Any | None,
                *, fixture: bool) -> None:
    timers = [s for s in record.statements if s.semanticType == "timer"]
    for s in record.statements:
        if s.semanticType == "eventbus_publish":
            s.framework = EVENT_FRAMEWORK
    if fixture:  # a fixture file is never an entry point
        for s in timers:
            _demote(s)
        return
    calls = _schedule_calls(root, source)
    for s in timers:
        call = next((c for c in calls if s.startLine <= c.start_point[0] + 1 <= s.endLine), None)
        if call is None:
            continue
        calls.remove(call)
        s.framework = SCHEDULE_FRAMEWORK
        s.handler = "execute"
        s.handlerLine, s.handlerPath = _locate(index, record, schedule_job_class(call, source))
