"""TOON (Token-Oriented Object Notation) serialization for structured-JSON capture.

The formatting is delegated to the reference **``toon-format``** library — a uniform
array-of-objects becomes a table (``members[2]{name,role}:`` + one comma-joined row each),
which is far denser than repeating ``members[i].field``; nested / non-uniform data falls
back to an indented block form. This module owns only the capture *safety* the library does
not provide: it produces a **sanitized copy** of the parsed JSON — secret-named keys
redacted (layer 1), secret-shaped string values redacted (layer 2, see `.redaction`), and
string leaves length-capped — and hands that to the library. Sanitizing before serializing
keeps the two concerns cleanly separated (and means a secret can never reach the encoder).

The whole document is kept: an oversized TOON is split into ordered parts downstream
(``emit.split``), never dropped here.

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

from .redaction import redact_secrets

IsSecret = Callable[[str], bool]


@dataclass
class _LeafCounter:
    """Tallies scalar leaves as the document is walked — informational only (no cap)."""

    count: int = 0


@dataclass
class Encoded:
    text: str
    leaf_count: int


def encode(obj: Any, *, is_secret: IsSecret, value_limit: int) -> Encoded:
    """Serialize ``obj`` to TOON. ``is_secret(key)`` → redact that leaf to ``***``; string
    leaves are truncated to ``value_limit`` (``<= 0`` disables). Every leaf is kept — an
    oversized result is split into parts downstream (``emit.split``), not dropped here."""
    counter = _LeafCounter()
    sanitized = _sanitize(obj, is_secret, value_limit, counter)
    return Encoded(toon_format.encode(sanitized), counter.count)


def _cap(value: Any, value_limit: int) -> Any:
    if isinstance(value, str) and value_limit > 0 and len(value) > value_limit:
        return value[:value_limit] + "…"
    return value


def _sanitize(
    value: Any,
    is_secret: IsSecret,
    value_limit: int,
    counter: _LeafCounter,
    key: str | None = None,
) -> Any:
    """Return a copy of ``value`` with secret leaves redacted and string leaves length-capped;
    ``counter`` tallies scalar leaves. Redaction keys on a scalar's *immediate* key, matching
    the flat capture's per-leaf rule."""
    if isinstance(value, dict):
        return {str(k): _sanitize(v, is_secret, value_limit, counter, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v, is_secret, value_limit, counter) for v in value]
    counter.count += 1
    if key is not None and is_secret(key):  # layer 1: redact by secret-looking key name
        return "***"
    if isinstance(value, str):  # layer 2: redact by secret-*shaped* value (see .redaction)
        value = redact_secrets(value)
    return _cap(value, value_limit)
