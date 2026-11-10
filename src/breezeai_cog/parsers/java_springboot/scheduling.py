"""Spring scheduled jobs: ``@Scheduled`` methods are ``timer`` bindings.

A method annotated with ``@Scheduled`` is the handler of a schedule; the schedule is its address::

  @Scheduled(cron = "0 2 * * *")            → timer  spring  PERIODIC  0 2 * * *
  @Scheduled(fixedRate = 5000)              → timer  spring  PERIODIC  5000
  @Scheduled(fixedDelayString = "${poll}")  → timer  spring  PERIODIC  <poll from config>
  @Scheduled(fixedRate = 5, timeUnit = TimeUnit.SECONDS)  → timer  spring  PERIODIC  5000

One record per ``@Scheduled`` (the annotation is repeatable, written either repeated or inside a
``@Schedules({…})`` container). The schedule is taken from
``cron``, then ``fixedRate``, then ``fixedDelay`` (or their ``…String`` forms); ``initialDelay``
and ``zone`` only qualify it. A literal, a ``static final`` constant or a ``${property}`` from the
module's base config resolves; anything else (SpEL ``#{…}``, an unset property) leaves
``endpoint`` empty. An interval is in milliseconds, as Spring's default: one given with
``timeUnit = TimeUnit.X`` is converted; a ``timeUnit`` that is not a literal unit, or a value
that is not a whole number of milliseconds, leaves ``endpoint`` empty. An ISO-8601 duration in a
``…String`` form (``fixedRateString = "PT5S"``) is kept as written: it carries its own unit.

Programmatic schedules are ``timer`` bindings too, read only on a receiver whose declared type is
a Spring scheduler (``TaskScheduler`` and its two implementations) or a
``ScheduledTaskRegistrar``; the JDK's ``ScheduledExecutorService`` has the same method names and
is never read::

  scheduler.scheduleAtFixedRate(this::poll, Duration.ofSeconds(30))  → timer  PERIODIC  30000
  registrar.addFixedDelayTask(this::report, 10000)                     → timer  PERIODIC  10000
  registrar.addCronTask(this::nightly, "0 0 2 * * *")                  → timer  PERIODIC  0 0 2 * * *
  registrar.addFixedDelayTask(new FixedDelayTask(this::sync, 60000, 0)) → timer  PERIODIC  60000

The period is the last argument (``Instant`` / ``Date`` start-time overloads put it last), in
milliseconds: a numeric literal, or ``Duration.ofMillis/ofSeconds/ofMinutes/ofHours/ofDays`` of
one. Any other period (a variable, a numeric constant: only ``String`` constants are folded)
leaves ``endpoint`` empty. A cron string resolves like ``@Scheduled``'s. The handler is the task argument when it
names a method (``this::poll``); a lambda gives its line only, anything else nothing.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord, Function
from ..emits import Emit
from ..treesitter import first_line, node_text
from ..vertx_common import handler_info, method_lines
from .messaging import _address, _ann_name, _annotations, _classes, typed_vars

#: Schedule attributes, in the order the first present one is taken as the address.
_SCHEDULE_ATTRS = ("cron", "fixedRate", "fixedRateString", "fixedDelay", "fixedDelayString")


def has_scheduled(source: bytes) -> bool:
    """Cheap gate: ``@Scheduled`` (also inside ``@Schedules``) is written in the file."""
    return b"@Scheduled" in source


#: Declared receiver type → its schedule methods (Spring 5.0+ ``TaskScheduler`` API and the
#: ``SchedulingConfigurer`` registrar). ``ScheduledExecutorService`` is deliberately absent.
_SCHEDULER_METHODS = {
    "TaskScheduler": ("scheduleAtFixedRate", "scheduleWithFixedDelay"),
    "ThreadPoolTaskScheduler": ("scheduleAtFixedRate", "scheduleWithFixedDelay"),
    "ConcurrentTaskScheduler": ("scheduleAtFixedRate", "scheduleWithFixedDelay"),
    "ScheduledTaskRegistrar": ("addFixedRateTask", "addFixedDelayTask", "addCronTask"),
}


#: A registrar task object (``addFixedRateTask(new FixedRateTask(task, period, …))``): its
#: constructor takes the task, then the period, or the cron expression for ``CronTask``.
_TASK_OBJECTS = frozenset({"FixedRateTask", "FixedDelayTask", "IntervalTask", "CronTask"})


def has_scheduler_calls(source: bytes) -> bool:
    """Cheap gate: the file names a Spring scheduler or registrar type."""
    return any(t.encode() in source for t in _SCHEDULER_METHODS)


def _number(node: Node, source: bytes) -> str | None:
    """A Java integer literal (``5000``, ``5_000L``) → its digits, else None."""
    if node.type not in ("decimal_integer_literal", "decimal_floating_point_literal"):
        return None
    digits = node_text(node, source).replace("_", "").rstrip("lLdDfF")
    return digits if digits.replace(".", "", 1).isdigit() else None


#: java.util.concurrent.TimeUnit → milliseconds per unit.
_UNIT_MS = {"NANOSECONDS": 1e-6, "MICROSECONDS": 1e-3, "MILLISECONDS": 1, "SECONDS": 1_000,
            "MINUTES": 60_000, "HOURS": 3_600_000, "DAYS": 86_400_000}


def _in_ms(value: str | None, unit: Node | None, source: bytes) -> str | None:
    """An interval ``value`` given in ``unit`` (``TimeUnit.SECONDS``) → milliseconds; ``unit``
    None → the value as is (Spring's default unit). Unknown unit or not a whole ms → None."""
    if unit is None or value is None:
        return value
    factor = _UNIT_MS.get(node_text(unit, source).rsplit(".", 1)[-1])
    try:
        ms = float(value) * factor if factor is not None else None
    except ValueError:
        return None  # a duration string ("PT5S") or other text: its unit is not this one
    return str(int(ms)) if ms is not None and ms == int(ms) else None


def _schedule(ann: Node, source: bytes, consts: dict[str, str],
              props: dict[str, str | None]) -> str | None:
    args = ann.child_by_field_name("arguments")
    pairs: dict[str, Node] = {}
    for kid in args.named_children if args is not None else []:
        if kid.type == "element_value_pair":
            key = kid.child_by_field_name("key")
            value = kid.child_by_field_name("value")
            if key is not None and value is not None:
                pairs[node_text(key, source)] = value
    for attr in _SCHEDULE_ATTRS:
        value = pairs.get(attr)
        if value is not None:
            found = _number(value, source) or _address(value, source, consts, props)
            return found if attr == "cron" else _in_ms(found, pairs.get("timeUnit"), source)
    return None


def _scheduled_annotations(member: Node, source: bytes) -> list[Node]:
    """The ``@Scheduled`` annotations of a method, in source order: written directly (repeatable)
    or as the elements of a ``@Schedules({…})`` / ``@Schedules(value = {…})`` container."""
    found: list[Node] = []
    for ann in _annotations(member):
        name = _ann_name(ann, source)
        if name == "Scheduled":
            found.append(ann)
        elif name == "Schedules":
            args = ann.child_by_field_name("arguments")
            for arg in args.named_children if args is not None else []:
                value = arg.child_by_field_name("value") if arg.type == "element_value_pair" \
                    else arg
                if value is None:
                    continue
                items = value.named_children if value.type == "element_value_array_initializer" \
                    else [value]
                found.extend(i for i in items if i.type == "annotation"
                             and _ann_name(i, source) == "Scheduled")
    return found


def detect_spring_scheduled(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str],
    props: dict[str, str | None],
) -> list[Emit]:
    """One ``timer`` per ``@Scheduled`` annotation on a method (also inside ``@Schedules``), for
    :func:`~..emits.apply`."""
    functions: dict[tuple[str, int], Function] = {(f.name, f.startLine): f for f in record.functions}
    out: list[Emit] = []
    for cls in _classes(root):
        body = cls.child_by_field_name("body")
        for member in body.named_children if body is not None else []:
            if member.type != "method_declaration":
                continue
            name_node = member.child_by_field_name("name")
            fn = functions.get((node_text(name_node, source), member.start_point[0] + 1)) \
                if name_node is not None else None
            if fn is None:
                continue
            for ann in _scheduled_annotations(member, source):
                out.append(Emit(ann, dict(
                    semanticType="timer",
                    method="PERIODIC",
                    endpoint=_schedule(ann, source, consts, props),
                    framework="spring",
                    handler=fn.name,
                    handlerLine=fn.startLine,
                ), parent=fn.id, node_type="synthetic",
                    text=first_line(node_text(ann, source))[:200],
                    line=fn.startLine, col=0, end_line=fn.endLine))
    return out


#: ``java.time.Duration`` factory → milliseconds per unit.
_DURATION_MS = {"ofMillis": 1, "ofSeconds": 1_000, "ofMinutes": 60_000, "ofHours": 3_600_000,
                "ofDays": 86_400_000}


def _whole_ms(node: Node, source: bytes) -> str | None:
    """A whole count written as a numeric literal → its digits, else None."""
    value = _number(node, source)
    return value if value is not None and value.isdigit() else None


def _period_ms(node: Node, source: bytes) -> str | None:
    """A period argument → milliseconds: ``30000``, or ``Duration.ofSeconds(30)``
    (``java.time.Duration`` qualified or not) of one. Anything else → None (honest-null)."""
    if node.type != "method_invocation":
        return _whole_ms(node, source)
    obj, name = node.child_by_field_name("object"), node.child_by_field_name("name")
    arg_list = node.child_by_field_name("arguments")
    args = list(arg_list.named_children) if arg_list is not None else []
    factor = _DURATION_MS.get(node_text(name, source)) if name is not None else None
    if obj is None or factor is None or len(args) != 1 \
            or node_text(obj, source) not in ("Duration", "java.time.Duration"):
        return None
    value = _whole_ms(args[0], source)
    return str(int(value) * factor) if value is not None else None


def _schedule_args(method: str, args: list[Node], source: bytes
                   ) -> tuple[Node | None, Node | None, Node | None]:
    """``(task, cron, period)`` arguments of a schedule call. ``(task, …, period)`` forms take the
    last argument as the period (the cron string for ``addCronTask``); a registrar given one
    ``new FixedRateTask(task, period, …)`` / ``CronTask(task, cron)`` reads its constructor.
    Anything else → no task: not a schedule this reader understands."""
    if len(args) >= 2:
        return (args[0], args[-1], None) if method == "addCronTask" else (args[0], None, args[-1])
    if len(args) != 1 or args[0].type != "object_creation_expression" \
            or not method.startswith("add"):
        return None, None, None
    typ = args[0].child_by_field_name("type")
    ctor = args[0].child_by_field_name("arguments")
    cargs = list(ctor.named_children) if ctor is not None else []
    name = node_text(typ, source).split("<", 1)[0].rsplit(".", 1)[-1] if typ is not None else ""
    if name not in _TASK_OBJECTS or len(cargs) < 2 \
            or any(c.type == "class_body" for c in args[0].named_children):
        return None, None, None
    return (cargs[0], cargs[1], None) if name == "CronTask" else (cargs[0], None, cargs[1])


def detect_spring_scheduler_calls(
    root: Node, source: bytes, record: FileRecord, consts: dict[str, str],
    props: dict[str, str | None],
) -> list[Emit]:
    """A ``timer`` for each schedule call on a Spring scheduler or registrar (by declared type),
    for :func:`~..emits.apply`: it enriches the unclassified statement the call begins
    (``starts``), else is a new record (a call inside a lambda)."""
    receivers = typed_vars(root, source, _SCHEDULER_METHODS)
    if not receivers:
        return []
    methods = method_lines(record.functions)
    found: list[Emit] = []

    def walk(n: Node) -> None:
        if n.type == "method_invocation":
            obj, name = n.child_by_field_name("object"), n.child_by_field_name("name")
            typ = receivers.get(node_text(obj, source).removeprefix("this.")) \
                if obj is not None else None
            method = node_text(name, source) if name is not None else ""
            arg_list = n.child_by_field_name("arguments")
            args = list(arg_list.named_children) if arg_list is not None else []
            task, cron, period = _schedule_args(method, args, source)
            if typ is not None and method in _SCHEDULER_METHODS[typ] and task is not None:
                endpoint = _address(cron, source, consts, props) if cron is not None \
                    else _period_ms(period, source) if period is not None else None
                handler, line = handler_info(task, source, methods)
                found.append(Emit(n, {
                    "semanticType": "timer", "framework": "spring", "method": "PERIODIC",
                    "endpoint": endpoint,
                }, fill={"handler": handler, "handlerLine": line}, enrich="starts"))
        for c in n.named_children:
            walk(c)

    walk(root)
    return found
