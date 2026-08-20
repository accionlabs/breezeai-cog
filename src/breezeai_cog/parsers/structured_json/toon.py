"""TOON (Token-Oriented Object Notation) serialization for structured-JSON capture.

The formatting is delegated to the reference **``toon-format``** library — a uniform
array-of-objects becomes a table (``members[2]{name,role}:`` + one comma-joined row each),
which is far denser than repeating ``members[i].field``; nested / non-uniform data falls
back to an indented block form. This module owns only the capture *safety* the library does
not provide: it produces a **redacted copy** of the parsed JSON — secret-named keys
redacted (layer 1) and secret-shaped string values redacted (layer 2, see `.redaction`) —
and hands that to the library. Redaction is the *only* transform: no value is truncated or
dropped, so the capture is lossless. Redacting before serializing keeps the two concerns
cleanly separated (and means a secret can never reach the encoder).

The whole document is kept; sizing happens once at emit
(``emit.split.split_oversized_statements``, backstopped by ``max_statement_parts``), never here.

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


def encode(obj: Any, *, is_secret: IsSecret) -> Encoded:
    """Serialize ``obj`` to TOON. ``is_secret(key)`` → redact that leaf to ``***``; string
    leaves are additionally passed through value-shape redaction. Nothing is truncated or
    dropped — an oversized result is split into parts at emit (``emit.split``), not here."""
    counter = _LeafCounter()
    redacted = _redact(obj, is_secret, counter)
    return Encoded(toon_format.encode(redacted), counter.count)


def _redact(
    value: Any,
    is_secret: IsSecret,
    counter: _LeafCounter,
    key: str | None = None,
) -> Any:
    """Return a copy of ``value`` with secret leaves redacted (nothing truncated or dropped);
    ``counter`` tallies scalar leaves. Redaction keys on a scalar's *immediate* key, matching
    the flat capture's per-leaf rule."""
    if isinstance(value, dict):
        return {str(k): _redact(v, is_secret, counter, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, is_secret, counter) for v in value]
    counter.count += 1
    if key is not None and is_secret(key):  # layer 1: redact by secret-looking key name
        return "***"
    if isinstance(value, str):  # layer 2: redact by secret-*shaped* value (see .redaction)
        return redact_secrets(value)
    return value
