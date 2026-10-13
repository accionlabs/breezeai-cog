"""Wiring facts: the shared shape of a fact one construct contributes to bindings elsewhere.

Wiring (contract §2) names no handler of its own; it adds a prefix, a guard, a registration or a
host to the bindings it covers, often from another file::

    app.use('/users', usersRouter());      // src/app.js:12 — prefix /users for usersRouter's file
    → Wiring(scope=("src/routes/users.js",), kind="prefix", value="/users", site="src/app.js:12")

A framework's registered fact (``register_fact``) collects :class:`Wiring` values per file and
resolves them with :func:`resolve_wiring`, which owns the rule every wiring fact shares: **one
value per scope**. A scope given two different values (a router mounted at two bases) is
ambiguous and dropped — its bindings keep their own path (honest-null), never a guessed prefix.
A scope given the same value twice keeps it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from .index_common import record_distinct

WiringKind = Literal["prefix", "guard", "registration", "host"]


@dataclass(frozen=True, slots=True)
class Wiring:
    """One wiring fact, as collected from the file that declares it."""

    #: What it applies to, as a tuple of keys: a file (``(path,)``), a router variable in a file
    #: (``(path, name)``), a module, a class, an app directory …
    scope: tuple[str, ...]
    kind: WiringKind
    #: The literal part (a prefix, a guard name); ``None`` = exists but unresolved.
    value: str | None
    #: ``file:line`` of the wiring statement (it stays a plain statement).
    site: str


def resolve_wiring(items: Iterable[Wiring]) -> dict[tuple[str, ...], str]:
    """Scope → value, one value per scope: a scope seen with two different values, or with an
    unresolved one, is dropped. Independent of the order of ``items``."""
    values: dict[tuple[str, ...], str | None] = {}
    for item in items:
        record_distinct(values, item.scope, item.value)
    return {scope: value for scope, value in values.items() if value is not None}


def compose_guards(*layers: list[str] | None) -> list[str] | None:
    """The ``guards`` of a binding from its guard layers, outermost first (app-wide middleware,
    then the router's, then the route's own): concatenated in that order, a name already present
    not repeated, ``None`` when there are none. Only guards known for certain to cover the binding
    belong in a layer (contract §5); a global guard never sets ``authRequired`` — that is the
    caller's rule, as only a route-level guard asserts auth."""
    out: list[str] = []
    for layer in layers:
        for name in layer or ():
            if name not in out:
                out.append(name)
    return out or None
