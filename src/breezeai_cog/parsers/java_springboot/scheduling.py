"""Spring scheduled jobs: ``@Scheduled`` methods are ``timer`` bindings.

A method annotated with ``@Scheduled`` is the handler of a schedule; the schedule is its address::

  @Scheduled(cron = "0 2 * * *")            → timer  spring  PERIODIC  0 2 * * *
  @Scheduled(fixedRate = 5000)              → timer  spring  PERIODIC  5000
  @Scheduled(fixedDelayString = "${poll}")  → timer  spring  PERIODIC  <poll from config>
  @Scheduled(fixedRate = 5, timeUnit = TimeUnit.SECONDS)  → timer  spring  PERIODIC  5000

One record per ``@Scheduled`` (the annotation is repeatable). The schedule is taken from
``cron``, then ``fixedRate``, then ``fixedDelay`` (or their ``…String`` forms); ``initialDelay``
and ``zone`` only qualify it. A literal, a ``static final`` constant or a ``${property}`` from the
module's base config resolves; anything else (SpEL ``#{…}``, an unset property) leaves
``endpoint`` empty. An interval is in milliseconds, as Spring's default: one given with
``timeUnit = TimeUnit.X`` is converted; a ``timeUnit`` that is not a literal unit, or a value
that is not a whole number of milliseconds, leaves ``endpoint`` empty. An ISO-8601 duration in a
``…String`` form (``fixedRateString = "PT5S"``) is kept as written: it carries its own unit.
"""

from __future__ import annotations

from tree_sitter import Node

from ...schemas import FileRecord, Function
from ..emits import Emit
from ..treesitter import first_line, node_text
from .messaging import _address, _ann_name, _annotations, _classes

#: Schedule attributes, in the order the first present one is taken as the address.
_SCHEDULE_ATTRS = ("cron", "fixedRate", "fixedRateString", "fixedDelay", "fixedDelayString")


def has_scheduled(source: bytes) -> bool:
    return b"@Scheduled" in source


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


def detect_spring_scheduled(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str],
    props: dict[str, str | None],
) -> list[Emit]:
    """One ``timer`` per ``@Scheduled`` annotation on a method, for :func:`~..emits.apply`."""
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
            for ann in _annotations(member):
                if _ann_name(ann, source) != "Scheduled":
                    continue
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
