"""Spring scheduled jobs: ``@Scheduled`` methods are ``timer`` bindings.

A method annotated with ``@Scheduled`` is the handler of a schedule; the schedule is its address::

  @Scheduled(cron = "0 2 * * *")            → timer  spring  PERIODIC  0 2 * * *
  @Scheduled(fixedRate = 5000)              → timer  spring  PERIODIC  5000
  @Scheduled(fixedDelayString = "${poll}")  → timer  spring  PERIODIC  <poll from config>

One record per ``@Scheduled`` (the annotation is repeatable). The schedule is taken from
``cron``, then ``fixedRate``, then ``fixedDelay`` (or their ``…String`` forms); ``initialDelay``
and ``zone`` only qualify it. A literal, a ``static final`` constant or a ``${property}`` from the
module's base config resolves; anything else (SpEL ``#{…}``, an unset property) leaves
``endpoint`` empty.
"""

from __future__ import annotations

from tree_sitter import Node

from ...emit import disambiguate, statement_id
from ...schemas import FileRecord, Function, Statement
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
            return _number(value, source) or _address(value, source, consts, props)
    return None


def detect_spring_scheduled(
    root: Node, source: bytes, path: str, record: FileRecord, consts: dict[str, str],
    props: dict[str, str | None],
) -> list[Statement]:
    """One ``timer`` per ``@Scheduled`` annotation on a method."""
    functions: dict[tuple[str, int], Function] = {(f.name, f.startLine): f for f in record.functions}
    seen = {s.id for s in record.statements}
    out: list[Statement] = []
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
                out.append(Statement(
                    id=disambiguate(statement_id(path, fn.startLine, 0), seen),
                    parentId=fn.id,
                    nodeType="synthetic",
                    semanticType="timer",
                    text=first_line(node_text(ann, source))[:200],
                    method="PERIODIC",
                    endpoint=_schedule(ann, source, consts, props),
                    framework="spring",
                    handler=fn.name,
                    handlerLine=fn.startLine,
                    startLine=fn.startLine,
                    endLine=fn.endLine,
                    path=path,
                ))
    return out
