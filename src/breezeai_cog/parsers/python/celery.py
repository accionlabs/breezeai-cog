"""Additive detector: Celery tasks, their senders and their beat schedules.

::

    @shared_task                                  → eventbus_consumer  celery  MESSAGE  proj.tasks.cleanup
    def cleanup(days): …
    cleanup.delay(30)                             → eventbus_send      celery           proj.tasks.cleanup
    app.send_task("reports.send")                 → eventbus_send      celery           reports.send
    "nightly": {"task": "proj.tasks.cleanup",     → timer              celery  PERIODIC 0 2 * * *
                "schedule": crontab(hour=2, minute=0)}                    handler cleanup
    sender.add_periodic_task(30.0, ping.s())      → timer              celery  PERIODIC 30000

* **Task** (binding, at its decorator): ``endpoint`` is its name from the ``celery.tasks`` fact
  (:mod:`.celery_tasks`) — given by ``name=``, or Celery's default name when the code confirms
  it; otherwise empty, never a guess.
* **Send** (outbound): ``.delay(…)`` / ``.apply_async(…)`` on a task of this file or one imported
  from another file (an aliased import is followed to the original name); ``send_task("…")``.
  ``endpoint`` is the task's name, written as its consumer writes it.
* **Beat entry / periodic task** (binding, a second trigger of the task, like a GraphQL
  subscription's topic): ``endpoint`` the schedule — ``crontab(…)`` as a cron expression
  (``minute hour day-of-month month day-of-week``, unset fields ``*``), seconds or a
  ``timedelta`` as milliseconds, as for Spring ``fixedRate``. A ``solar`` schedule or a
  non-literal → empty. ``handler`` the task function, when its name or reference leads to one.
"""

from __future__ import annotations

from tree_sitter import Node

from ..additive import DetectContext, Detection, Detector, Gate, register_detector
from ..emits import Emit
from ..treesitter import first_line, node_text
from .celery_tasks import (
    UNKNOWN_NAME,
    beat_entries, celery_tasks, kwarg, positional, string, task_defs, walk,
)

_SENDS = frozenset({"delay", "apply_async"})
_SIGNATURES = frozenset({"s", "si", "signature"})
#: ``crontab`` parameters in positional order, and their place in a cron expression.
_CRONTAB = ("minute", "hour", "day_of_week", "day_of_month", "month_of_year")
_CRON_ORDER = ("minute", "hour", "day_of_month", "month_of_year", "day_of_week")
_TIMEDELTA_MS = {"weeks": 604_800_000, "days": 86_400_000, "hours": 3_600_000,
                 "minutes": 60_000, "seconds": 1000, "milliseconds": 1}


def _number(node: Node | None, source: bytes) -> float | None:
    if node is None or node.type not in ("integer", "float"):
        return None
    try:
        return float(node_text(node, source).replace("_", ""))
    except ValueError:
        return None


def _ms(seconds: float) -> str | None:
    ms = seconds * 1000
    return str(int(ms)) if ms == int(ms) and ms > 0 else None


def _cron_field(node: Node | None, source: bytes) -> str | None:
    """One ``crontab`` argument: a string (``"*/15"``, ``"mon-fri"``) or an integer; unset → ``*``."""
    if node is None:
        return "*"
    if node.type == "integer":
        return node_text(node, source)
    return string(node, source) or None


def schedule(node: Node | None, source: bytes) -> str | None:
    """A beat schedule as an ``endpoint`` (see the module docstring); None when not literal."""
    if node is None:
        return None
    seconds = _number(node, source)
    if seconds is not None:
        return _ms(seconds)
    fn = node.child_by_field_name("function") if node.type == "call" else None
    if fn is None:
        return None
    callee = node_text(fn, source).rsplit(".", 1)[-1]
    args = positional(node)
    if callee == "crontab":
        if len(args) > len(_CRONTAB):
            return None
        given = dict(zip(_CRONTAB, args))
        fields = {name: _cron_field(given.get(name) or kwarg(node, name, source), source)
                  for name in _CRONTAB}
        if any(v is None for v in fields.values()):
            return None
        return " ".join(fields[n] or "" for n in _CRON_ORDER)
    if callee == "timedelta":
        if args:  # positional timedelta(days, seconds, …) — not read
            return None
        total = 0.0
        for unit, ms in _TIMEDELTA_MS.items():
            value = kwarg(node, unit, source)
            if value is not None:
                n = _number(value, source)
                if n is None:
                    return None
                total += n * ms
        return str(int(total)) if total > 0 and total == int(total) else None
    if callee == "schedule":
        return schedule(args[0] if args else kwarg(node, "run_every", source), source)
    return None


def _import_aliases(root: Node, source: bytes) -> dict[str, str]:
    """``from m import cleanup as c`` → ``{"c": "cleanup"}``."""
    out: dict[str, str] = {}
    for node in walk(root):
        if node.type != "import_from_statement":
            continue
        for child in node.named_children:
            if child.type == "aliased_import":
                name, alias = child.child_by_field_name("name"), child.child_by_field_name("alias")
                if name is not None and alias is not None:
                    out[node_text(alias, source)] = node_text(name, source).rsplit(".", 1)[-1]
    return out


#: Cheap pre-check: a file with none of these has nothing for this detector.
_MARKERS = (b"celery", b"CELERY", b"shared_task", b".delay(", b".apply_async(", b"send_task",
            b"beat_schedule", b"add_periodic_task")


def _detect(dc: DetectContext) -> Detection | None:
    source, path, record = dc.source, dc.ctx.path, dc.record
    if not any(m in source for m in _MARKERS):
        return None
    fact = celery_tasks(dc.ctx.resolution_index)
    by_file = fact.by_file if fact is not None else {}
    by_name = fact.by_name if fact is not None else {}
    bindings = dc.bindings or {}
    aliases = _import_aliases(dc.root, source)
    functions = {(f.name, f.startLine): f for f in record.functions}
    found: list[Emit] = []

    defs = task_defs(dc.root, source)
    mine = {t.function: t.definition.start_point[0] + 1 for t in defs}
    for t in defs:
        line = t.definition.start_point[0] + 1
        fn = functions.get((t.function, line))
        name = by_file.get(path, {}).get(t.function)
        if name is None and fact is None and t.explicit != UNKNOWN_NAME:
            name = t.explicit  # no index (single-file parsing): name= only
        found.append(Emit(
            t.decorator,
            {"semanticType": "eventbus_consumer", "framework": "celery", "method": "MESSAGE",
             "endpoint": name, "handler": t.function, "handlerLine": line},
            parent=fn.id if fn is not None else None, node_type="synthetic",
            text=first_line(node_text(t.decorator, source)),
        ))

    def task(expr: Node | None) -> tuple[str, str] | None:
        """The (file, function) of a task expression: ``cleanup`` (this file's, or imported) or
        ``tasks.cleanup`` (an imported module's)."""
        if expr is None:
            return None
        if expr.type == "identifier":
            local = node_text(expr, source)
            if local in mine and local not in bindings:
                return path, local
            target = bindings.get(local)
            original = aliases.get(local, local)
            if target is not None and original in by_file.get(target, {}):
                return target, original
        elif expr.type == "attribute":
            obj, attr = expr.child_by_field_name("object"), expr.child_by_field_name("attribute")
            module = node_text(obj, source) if obj is not None and obj.type == "identifier" else ""
            target = bindings.get(module)
            if target is not None and target.endswith("__init__.py"):
                # `from proj import tasks` binds the package; the module is proj/tasks.py
                sub = f"{target.removesuffix('__init__.py')}{module}.py"
                target = sub if sub in by_file else target
            if target is not None and attr is not None and \
                    node_text(attr, source) in by_file.get(target, {}):
                return target, node_text(attr, source)
        return None

    def name_of(ref: tuple[str, str]) -> str | None:
        return by_file.get(ref[0], {}).get(ref[1])

    def timer(node: Node, endpoint: str | None, ref: tuple[str, str] | None) -> None:
        line = mine.get(ref[1]) if ref is not None and ref[0] == path else None
        found.append(Emit(
            node, {"semanticType": "timer", "framework": "celery", "method": "PERIODIC",
                   "endpoint": endpoint, "handler": ref[1] if ref else None, "handlerLine": line},
            node_type="synthetic", text=first_line(node_text(node, source)),
        ))

    for node in walk(dc.root):
        if node.type != "call":
            continue
        fn_node = node.child_by_field_name("function")
        if fn_node is None or fn_node.type != "attribute":
            continue
        attr = fn_node.child_by_field_name("attribute")
        method = node_text(attr, source) if attr is not None else ""
        if method in _SENDS:
            ref = task(fn_node.child_by_field_name("object"))
            if ref is not None:
                found.append(Emit(node, {"semanticType": "eventbus_send", "framework": "celery",
                                         "endpoint": name_of(ref)}, enrich="own"))
        elif method == "send_task":
            args = positional(node)
            found.append(Emit(node, {"semanticType": "eventbus_send", "framework": "celery",
                                     "endpoint": string(args[0], source) if args else None},
                              enrich="own"))
        elif method == "add_periodic_task":
            args = positional(node)
            sig = args[1] if len(args) > 1 else kwarg(node, "sig", source)
            ref = None
            if sig is not None and sig.type == "call":
                sig_fn = sig.child_by_field_name("function")
                sig_attr = sig_fn.child_by_field_name("attribute") if sig_fn is not None and \
                    sig_fn.type == "attribute" else None
                if sig_fn is not None and sig_attr is not None and \
                        node_text(sig_attr, source) in _SIGNATURES:
                    ref = task(sig_fn.child_by_field_name("object"))
                elif sig_fn is not None and node_text(sig_fn, source).rsplit(".", 1)[-1] == "signature":
                    sig_args = positional(sig)
                    named = string(sig_args[0], source) if sig_args else None
                    ref = by_name.get(named) if named else None
            timer(node, schedule(args[0] if args else kwarg(node, "schedule", source), source), ref)

    for entry, task_node, schedule_node in beat_entries(dc.root, source):
        named = string(task_node, source)
        timer(entry, schedule(schedule_node, source), by_name.get(named) if named else None)

    if not found:
        return None
    found.sort(key=lambda e: (e.start_line, e.node.start_point[1]))
    return Detection(found, label="celery")


# An entry-point emitter, so fixture/test files are skipped.
register_detector(Detector(
    name="celery",
    gate=Gate("python", order=10, skip_fixtures=True),
    detect=_detect, frameworks=("celery",),
))
