"""TOON (Token-Oriented Object Notation) serialization for structured-JSON capture.

The formatting is delegated to the reference **``toon-format``** library — a uniform
array-of-objects becomes a table (``members[2]{name,role}:`` + one comma-joined row each),
which is far denser than repeating ``members[i].field``; nested / non-uniform data falls
back to an indented block form. This module owns only the capture *safety* the library does
not provide: it produces a **sanitized copy** of the parsed JSON — secret-named keys
redacted, string leaves length-capped, total leaf count bounded — and hands that to the
library. Sanitizing before serializing keeps the two concerns cleanly separated (and means a
secret can never reach the encoder).

Example — ``{"team":"pay","members":[{"name":"Al","role":"admin"},
{"name":"Bo","role":"viewer"}]}`` encodes to::

    team: pay
    members[2]{name,role}:
      Al,admin
      Bo,viewer
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import toon_format

IsSecret = Callable[[str], bool]

#: Sentinel: a leaf dropped because the leaf-count bound was reached.
_OMIT: Any = object()


@dataclass
class _Budget:
    """Bounds the leaf count so a pathological document can't blow up the output."""

    max_leaves: int
    count: int = 0
    truncated: bool = False
    stopped: bool = False

    def take(self) -> bool:
        if self.count >= self.max_leaves:
            self.truncated = True
            self.stopped = True
            return False
        self.count += 1
        return True


@dataclass
class Encoded:
    text: str
    leaf_count: int
    truncated: bool


def encode(obj: Any, *, is_secret: IsSecret, value_limit: int, max_leaves: int) -> Encoded:
    """Serialize ``obj`` to TOON. ``is_secret(key)`` → redact that leaf to ``***``; string
    leaves are truncated to ``value_limit`` (``<= 0`` disables); the total leaf count is
    capped at ``max_leaves`` (``Encoded.truncated`` signals the cap was hit)."""
    budget = _Budget(max_leaves)
    sanitized = _sanitize(obj, is_secret, value_limit, budget)
    if sanitized is _OMIT:  # only when max_leaves <= 0 (claims() already excludes root scalars)
        sanitized = None
    return Encoded(toon_format.encode(sanitized), budget.count, budget.truncated)


def _cap(value: Any, value_limit: int) -> Any:
    if isinstance(value, str) and value_limit > 0 and len(value) > value_limit:
        return value[:value_limit] + "…"
    return value


def _sanitize(
    value: Any,
    is_secret: IsSecret,
    value_limit: int,
    budget: _Budget,
    key: str | None = None,
) -> Any:
    """Return a copy of ``value`` with secret leaves redacted, strings capped, and leaves
    beyond the budget pruned (returns ``_OMIT`` for a leaf that trips the bound). Redaction
    keys on a scalar's *immediate* key, matching the flat capture's per-leaf rule."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if budget.stopped:
                break
            child = _sanitize(v, is_secret, value_limit, budget, str(k))
            if child is not _OMIT:
                out[str(k)] = child
        return out
    if isinstance(value, list):
        items: list[Any] = []
        for v in value:
            if budget.stopped:
                break
            child = _sanitize(v, is_secret, value_limit, budget)
            if child is not _OMIT:
                items.append(child)
        return items
    if not budget.take():
        return _OMIT
    if key is not None and is_secret(key):
        return "***"
    return _cap(value, value_limit)
