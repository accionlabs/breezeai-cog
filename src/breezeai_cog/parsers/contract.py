"""Check emitted entry points against the entry-point contract (``docs/architecture/entry-points.md``).

:func:`validate` returns the contract violations in one file record. It is a check, never a
transform: it runs in tests, in golden comparisons and under ``--verbose`` (a ``contract.violation``
debug line per finding), and never changes or filters output.

A violation is an **error** (a value that is wrong) or a **gap** (a value that is missing). Wrong
is worse than missing (§8): a parser leaves a handler empty when it cannot name it for certain
(a factory call's result, a page made of two components), exactly as it leaves an unresolved
``endpoint`` empty. Tests fail on errors; gaps are counted, as a completeness measure.

The entry points are the ``route``, ``eventbus_consumer`` and ``timer`` records whose
``routeKind`` is not ``field_resolver``, ``mount``, ``layout`` or ``navigation`` (contract §5).
Mounts, layouts and links are written in the Target Spec's current form — ``route`` with that
``routeKind`` — until its entry-point proposal is approved; they are not entry points. A link
(``routeKind: navigation``) is outbound and checked like one.

=============== ======== =================================================================
Rule            Severity Violation
=============== ======== =================================================================
``method``      error    ``method`` outside the contract's value set (§5)
``routeKind``   error    missing on a ``route``, or outside the value set for its semantic
                         type (§5)
``framework``   error    missing (§5)
``endpoint``    error    an empty string instead of no value, or a code placeholder
                         (``${…}``, ``#{…}``, a backtick) instead of a resolved address
                         (§6, §8)
``one-record``  error    two entry points with the same file, line, method, endpoint and
                         handler (§8); one address serving several handlers (a Vert.x
                         service proxy's methods) is several bindings
``guards``      error    an empty list, an empty name or a repeated name (§5)
``handler``     gap      neither ``handler`` nor ``handlerLine``, except for a URL served
                         by framework code (§4, §8 "every record leads to its handler")
``outbound``    error    an outbound record (``api_call``, a ``route`` link with
                         ``routeKind: navigation``, ``eventbus_send`` / ``_publish``)
                         whose ``endpoint`` is an empty
                         string or a code placeholder — it must be written like the entry
                         point it reaches (§6)
=============== ======== =================================================================
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..schemas import FileRecord, Statement
from .aspnet_common import FRAMEWORK_SERVED

ENTRY_POINT_TYPES = frozenset({"route", "eventbus_consumer", "timer"})

#: ``routeKind`` values filed under ``route`` that are not entry points (see the docstring).
NOT_ENTRY_POINT_KINDS = frozenset({"field_resolver", "mount", "layout", "navigation"})

HTTP_METHODS = frozenset({"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"})
METHODS = HTTP_METHODS | frozenset({
    "ANY", "WEBSOCKET",
    "QUERY", "MUTATION", "SUBSCRIPTION", "RESOLVE_REFERENCE",
    "RPC",
    "MESSAGE", "EVENT", "CONSUMER",
    "TIMER", "PERIODIC",
})

ROUTE_KINDS: dict[str, frozenset[str | None]] = {
    "route": frozenset({"route", "rpc", "ws", "page", "query", "mutation", "subscription"}),
    "eventbus_consumer": frozenset({None, "message"}),
    "timer": frozenset({None}),
}

_PLACEHOLDERS = ("${", "#{", "`")

#: Rules whose violation is a missing value, not a wrong one.
_GAPS = frozenset({"handler"})


@dataclass(frozen=True, slots=True)
class Violation:
    rule: str
    #: ``error`` (a wrong value) or ``gap`` (a missing one).
    severity: str
    statement_id: str
    detail: str


#: Outbound semantic types: their address is written like the entry point they reach (§6).
OUTBOUND_TYPES = frozenset({"api_call", "eventbus_send", "eventbus_publish"})


def _bad_address(endpoint: str | None) -> str | None:
    if endpoint == "":
        return "empty string; an unresolved address has no value"
    if endpoint is not None and any(p in endpoint for p in _PLACEHOLDERS):
        return endpoint
    return None


def is_entry_point(s: Statement) -> bool:
    return s.semanticType in ENTRY_POINT_TYPES and s.routeKind not in NOT_ENTRY_POINT_KINDS


def _framework_served(s: Statement) -> bool:
    return any(name in s.text for name in FRAMEWORK_SERVED)


def _check(s: Statement) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if s.method is not None and s.method not in METHODS:
        found.append(("method", s.method))
    kinds = ROUTE_KINDS[s.semanticType or ""]
    if s.routeKind not in kinds:
        found.append(("routeKind", repr(s.routeKind)))
    if not s.framework:
        found.append(("framework", "missing"))
    bad = _bad_address(s.endpoint)
    if bad is not None:
        found.append(("endpoint", bad))
    if not s.handler and s.handlerLine is None and not _framework_served(s):
        found.append(("handler", "no handler and no handlerLine"))
    if s.guards is not None and (
        not s.guards or not all(s.guards) or len(set(s.guards)) != len(s.guards)
    ):
        found.append(("guards", repr(s.guards)))
    return found


def validate(record: FileRecord) -> list[Violation]:
    """Contract violations among ``record``'s entry points, in statement order."""
    out: list[Violation] = []
    seen: Counter[tuple[int, str | None, str | None, str | None]] = Counter()
    for s in record.statements:
        if s.semanticType in OUTBOUND_TYPES or s.routeKind == "navigation":
            bad = _bad_address(s.endpoint)
            if bad is not None:
                out.append(Violation("outbound", "error", s.id, bad))
            continue
        if not is_entry_point(s):
            continue
        out.extend(
            Violation(rule, "gap" if rule in _GAPS else "error", s.id, detail)
            for rule, detail in _check(s)
        )
        key = (s.startLine, s.method, s.endpoint, s.handler)
        seen[key] += 1
        if seen[key] == 2:
            out.append(Violation(
                "one-record", "error", s.id,
                f"line {s.startLine} {s.method} {s.endpoint!r} recorded twice",
            ))
    return out
