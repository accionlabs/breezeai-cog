"""Additive detector: Celery tasks, their senders and their beat schedules.

::

    @shared_task                                  → eventbus_consumer  celery  MESSAGE  proj.tasks.cleanup
    def cleanup(days): …
    class Report(Task): name = "proj.report"      → eventbus_consumer  celery  MESSAGE  proj.report
                        def run(self): …                                               handler run
    app.register_task(Report())
    cleanup.delay(30)                             → eventbus_send      celery           proj.tasks.cleanup
    app.send_task("reports.send")                 → eventbus_send      celery           reports.send
    chain(cleanup.s(30), ping.si())()             → eventbus_send ×2   (one per task, at its signature)
    "nightly": {"task": "proj.tasks.cleanup",     → timer              celery  PERIODIC 0 2 * * *
                "schedule": crontab(hour=2, minute=0)}                    handler cleanup
    sender.add_periodic_task(30.0, ping.s())      → timer              celery  PERIODIC 30000

* **Task** (binding, at its decorator): ``@shared_task`` or ``@<app>.task`` on a Celery app —
  never ``@scheduler.task``. ``endpoint`` is its name from the ``celery.tasks`` fact
  (:mod:`.task_names`) — given by ``name=``, or Celery's default name when the code confirms
  it; otherwise empty, never a guess. A **class task** (base ``Task`` from celery, registered on
  a Celery app with ``register_task``) sits at its class, ``handler`` its ``run`` method.
* **Send** (outbound): ``.delay(…)`` / ``.apply_async(…)`` on a task of this file or one imported
  from another file (an aliased import is followed to the original name); ``send_task("…")`` on a
  Celery app (``Celery(…)`` of this file or of the file it is imported from, or
  ``celery.current_app`` — not any object with a ``send_task`` method).
  ``endpoint`` is the task's name, written as its consumer writes it.
* **Canvas send** (outbound): a signature — ``task.s(…)`` / ``.si(…)`` / ``.signature(…)``, or
  celery's ``signature("name")`` — is a send only when it is run: ``.delay`` / ``.apply_async``
  on it (one record, at that call); inside a celery ``chain`` / ``group`` / ``chord`` or a ``|``
  expression that is called or ``.delay`` / ``.apply_async``-ed; or passed as ``link=`` to
  ``apply_async`` (one record per signature, at it). A canvas stored in a variable and run later
  is not followed — no record.
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
from .task_names import (
    UNKNOWN_NAME,
    Receivers, beat_entries, celery_tasks, class_tasks, is_app, kwarg, positional, registrations,
    string, task_defs, walk,
)

_SENDS = frozenset({"delay", "apply_async"})
_SIGNATURES = frozenset({"s", "si", "signature"})
#: Celery's canvas primitives and its by-name signature, wherever celery exports them.
_CANVAS = frozenset({f"{m}.{c}" for m in ("celery", "celery.canvas") for c in ("chain", "group", "chord")})
_CHORD = frozenset({"celery.chord", "celery.canvas.chord"})
_NAMED_SIGNATURE = frozenset({f"{m}.{c}" for m in ("celery", "celery.canvas")
                              for c in ("signature", "Signature")})
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
            b"beat_schedule", b"add_periodic_task", b"register_task")


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

    apps = fact.apps if fact is not None else {}
    receivers = Receivers(dc.root, source, dc.ctx.repo_root, path)
    defs = [t for t in task_defs(dc.root, source) if is_app(receivers.task_ref(t), apps)]
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
            parent=fn.id if fn is not None else None,
            text=first_line(node_text(t.decorator, source)),
        ))

    # Class tasks: registered on a Celery app (the fact; without it, in this file only).
    classes = class_tasks(dc.root, source, receivers.origins)
    if fact is not None:
        names: dict[str, str | None] = fact.classes.get(path, {})
    else:
        local = {c.name for c in classes}
        mine_registered = {cls for cls, app in registrations(dc.root, source, path, receivers, local)
                           if cls[0] == path and is_app(app, {})}
        names = {c.name: (c.explicit if c.explicit != UNKNOWN_NAME else None)
                 for c in classes if (path, c.name) in mine_registered}
    run_lines: dict[str, int] = {}  # a class task's name → its run method's line
    for c in classes:
        if c.name not in names:
            continue
        run_line = c.run.start_point[0] + 1 if c.run is not None else None
        run_fn = functions.get(("run", run_line)) if run_line is not None else None
        if names[c.name] is not None and run_line is not None:
            run_lines[str(names[c.name])] = run_line
        found.append(Emit(
            c.definition,
            {"semanticType": "eventbus_consumer", "framework": "celery", "method": "MESSAGE",
             "endpoint": names[c.name], "handler": "run" if c.run is not None else None,
             "handlerLine": run_line},
            parent=run_fn.id if run_fn is not None else None,
            text=first_line(node_text(c.definition, source)),
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

    def timer(node: Node, endpoint: str | None, ref: tuple[str, str] | None,
              named: str | None = None) -> None:
        line = None
        if ref is not None and ref[0] == path:
            line = run_lines.get(named) if named in run_lines else mine.get(ref[1])
        found.append(Emit(
            node, {"semanticType": "timer", "framework": "celery", "method": "PERIODIC",
                   "endpoint": endpoint, "handler": ref[1] if ref else None, "handlerLine": line},
            text=first_line(node_text(node, source)),
        ))

    origins = receivers.origins

    def celery_name(callee: Node | None, wanted: frozenset[str]) -> bool:
        """Whether ``callee`` names one of celery's ``wanted`` (``chain``, not itertools')."""
        if callee is None or callee.type not in ("identifier", "attribute"):
            return False
        text = node_text(callee, source)
        qualified = origins.qualified(text)
        if qualified is None:  # not imported by name: only `from celery import *` explains it
            return "celery" in origins.star and f"celery.{text}" in wanted
        return qualified in wanted

    def unwrap(expr: Node | None) -> Node | None:
        while expr is not None and expr.type == "parenthesized_expression" and expr.named_children:
            expr = expr.named_children[0]
        return expr

    def signature(expr: Node | None) -> tuple[str | None] | None:
        """``(endpoint,)`` when ``expr`` is a signature of a task (``t.s(…)``, ``t.si(…)``,
        ``t.signature(…)``) or by name (``signature("name")``, ``app.signature("name")``)."""
        fn = expr.child_by_field_name("function") if expr is not None and expr.type == "call" else None
        if expr is None or fn is None:
            return None
        if fn.type == "attribute":
            attr, obj = fn.child_by_field_name("attribute"), fn.child_by_field_name("object")
            method = node_text(attr, source) if attr is not None else ""
            if method in _SIGNATURES:
                ref = task(obj)
                if ref is not None:
                    return (name_of(ref),)
            if method != "signature" or not is_app(receivers.ref(obj), apps):
                if not celery_name(fn, _NAMED_SIGNATURE):
                    return None
        elif not celery_name(fn, _NAMED_SIGNATURE):
            return None
        args = positional(expr)
        return (string(args[0], source) if args else None,)

    def canvas(expr: Node | None) -> bool:
        """A celery ``chain`` / ``group`` / ``chord`` call, or a ``|`` of signatures."""
        expr = unwrap(expr)
        if expr is None:
            return False
        if expr.type == "call":
            return celery_name(expr.child_by_field_name("function"), _CANVAS)
        if expr.type == "binary_operator":
            op = expr.child_by_field_name("operator")
            return op is not None and node_text(op, source) == "|" and any(
                signature(unwrap(side)) is not None or canvas(side)
                for side in (expr.child_by_field_name("left"), expr.child_by_field_name("right")))
        return False

    sent: set[tuple[int, int]] = set()

    def run(expr: Node | None) -> None:
        """Every signature in a canvas (or a ``link=`` value) that is run: one send each."""
        expr = unwrap(expr)
        if expr is None:
            return
        sig = signature(expr)
        if sig is not None:
            if (expr.start_byte, expr.end_byte) not in sent:
                sent.add((expr.start_byte, expr.end_byte))
                found.append(Emit(expr, {"semanticType": "eventbus_send", "framework": "celery",
                                         "endpoint": sig[0]}))
        elif expr.type in ("list", "tuple"):
            for el in expr.named_children:
                run(el)
        elif expr.type in ("generator_expression", "list_comprehension"):
            run(expr.child_by_field_name("body"))
        elif expr.type == "binary_operator" and canvas(expr):
            run(expr.child_by_field_name("left"))
            run(expr.child_by_field_name("right"))
        elif expr.type == "call" and canvas(expr):
            for arg in positional(expr):
                run(arg)
            body = kwarg(expr, "body", source)  # chord(header, body=…)
            if body is not None:
                run(body)

    for node in walk(dc.root):
        if node.type != "call":
            continue
        fn_node = node.child_by_field_name("function")
        if fn_node is not None and canvas(fn_node):  # chain(…)(), (a.s() | b.s())()
            run(fn_node)
            callee = unwrap(fn_node)
            if callee is not None and callee.type == "call" and \
                    celery_name(callee.child_by_field_name("function"), _CHORD):
                for arg in positional(node):  # chord(header)(body)
                    run(arg)
            continue
        if fn_node is None or fn_node.type != "attribute":
            continue
        attr = fn_node.child_by_field_name("attribute")
        method = node_text(attr, source) if attr is not None else ""
        if method in _SENDS:
            obj = fn_node.child_by_field_name("object")
            ref = task(obj)
            signed = signature(unwrap(obj)) if ref is None else None  # t.s(…).delay()
            if ref is not None or signed is not None:
                endpoint = name_of(ref) if ref is not None else signed[0] if signed else None
                found.append(Emit(node, {"semanticType": "eventbus_send", "framework": "celery",
                                         "endpoint": endpoint}, enrich="own"))
            elif canvas(obj):
                run(obj)
            if method == "apply_async" and (ref is not None or signed is not None or canvas(obj)):
                run(kwarg(node, "link", source))
        elif method == "send_task":
            if not is_app(receivers.ref(fn_node.child_by_field_name("object")), apps):
                continue  # a user class's own `send_task` method
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
        timer(entry, schedule(schedule_node, source), by_name.get(named) if named else None, named)

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
