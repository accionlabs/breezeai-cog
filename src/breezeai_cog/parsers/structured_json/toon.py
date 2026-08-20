"""TOON (Token-Oriented Object Notation) serialization for structured-JSON capture.

The formatting is delegated to the reference **``toon-format``** library — a uniform
array-of-objects becomes a table (``members[2]{name,role}:`` + one comma-joined row each),
which is far denser than repeating ``members[i].field``; nested / non-uniform data falls
back to an indented block form. This module owns only the capture *safety* the library does
not provide: it produces a **redacted copy** of the parsed JSON — secret-named keys redacted
(layer 1), secret-shaped string values redacted (layer 2, see `.redaction`) — and hands that
to the library. Redacting before serializing keeps the two concerns cleanly separated (and
means a secret can never reach the encoder).

**Size is not this module's concern.** The full document is encoded here; sizing happens once
at the emit choke point (``emit.split.split_oversized_statements``), which slices an oversized
statement into ordered ``#partNofN`` records that concatenate back byte-for-byte, with
``max_statement_parts`` as the node-explosion backstop. This module previously also capped
each string leaf and bounded the total leaf count — both were size control predating the
splitter, and both were *lossy and silent*: a clipped value kept its head, so a presence check
still passed while the tail was gone. They are deliberately absent now, so a captured document
is always complete.

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
class Encoded:
    text: str
    leaf_count: int


def encode(obj: Any, *, is_secret: IsSecret) -> Encoded:
    """Serialize ``obj`` to TOON in full. A leaf is redacted to ``***`` when its key looks
    secret (layer 1) or its value has a recognizable secret shape (layer 2). Nothing is
    truncated or dropped; ``Encoded.leaf_count`` reports how many scalar leaves the document
    holds (counted during the redaction walk, so it is free)."""
    counter = _LeafCounter()
    redacted = _redact(obj, is_secret, counter)
    return Encoded(toon_format.encode(redacted), counter.count)


@dataclass
class _LeafCounter:
    """Counts scalar leaves during the redaction walk — reporting only, never a bound."""

    count: int = 0


def _redact(
    value: Any,
    is_secret: IsSecret,
    counter: _LeafCounter,
    key: str | None = None,
) -> Any:
    """Return a copy of ``value`` with secret leaves replaced by ``***``, counting scalar
    leaves on the way. Layer 1 keys on a scalar's *immediate* key name, matching the flat
    capture's per-leaf rule; layer 2 then redacts secret-*shaped* string values in place."""
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
