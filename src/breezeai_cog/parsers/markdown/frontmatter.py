"""YAML front matter of a Markdown document, loaded defensively.

The parsed front matter is written into ``File.metadata``, which the main process serializes
for every record. ``yaml.safe_load`` alone is not safe for that: an alias tree ("billion laughs")
is cheap to load but explodes when written out, a recursive alias yields a circular structure,
and ``!!binary`` yields ``bytes``. Any of those would fail the whole run at the sink, not just
this file. So anchors / aliases are rejected, the block is size-capped, and the result is
reduced to plain JSON values (bounded depth and width) here, in the worker.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any

import yaml  # type: ignore[import-untyped]

from ..structured_json.redaction import redact_secrets

#: Front matter larger than this is not parsed (treated as invalid). Real skill / agent /
#: command headers are a few hundred bytes.
MAX_FRONT_MATTER_BYTES = 8 * 1024
_MAX_DEPTH = 3
_MAX_ITEMS = 50
_MAX_STRING = 4000

#: Case-insensitive substrings of a front-matter key whose value is replaced by ``***`` — the
#: key-name layer of the structured-JSON redaction, applied to document headers.
_CREDENTIAL_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "authorization",
    "credential",
    "private_key",
    "privatekey",
)

_DELIMITERS = ("---",)
_CLOSERS = ("---", "...")


class _NoAliasLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader that refuses anchors and aliases (alias bombs, recursive structures)."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise yaml.YAMLError("anchors and aliases are not allowed in front matter")
        return super().compose_node(parent, index)


def split_front_matter(lines: list[str]) -> tuple[str | None, int]:
    """Return ``(front_matter_text, body_start_index)``. Front matter is a block that opens on
    the first line with ``---`` and closes on the next ``---`` / ``...`` line. Without one,
    returns ``(None, 0)``; an unclosed block is not front matter."""
    if not lines or lines[0].strip() not in _DELIMITERS:
        return None, 0
    for i in range(1, len(lines)):
        if lines[i].strip() in _CLOSERS:
            return "\n".join(lines[1:i]), i + 1
    return None, 0


def load_front_matter(text: str | None) -> tuple[dict[str, Any], bool]:
    """Parse front matter → ``(mapping, ok)``. ``ok`` is False when a block was present but
    could not be used (too large, invalid YAML, anchors / aliases, or not a mapping); the
    mapping is then empty. No front matter at all is ``({}, True)``."""
    if text is None:
        return {}, True
    if len(text.encode("utf-8")) > MAX_FRONT_MATTER_BYTES:
        return {}, False
    try:
        data = yaml.load(text, Loader=_NoAliasLoader)  # noqa: S506 — SafeLoader subclass
    except (yaml.YAMLError, ValueError, RecursionError):
        return {}, False
    if data is None:
        return {}, True
    if not isinstance(data, dict):
        return {}, False
    return _mapping(data, 1), True


def _secret_key(key: str) -> bool:
    low = key.lower()
    return any(token in low for token in _CREDENTIAL_TOKENS)


def _mapping(data: dict[Any, Any], depth: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in list(data.items())[:_MAX_ITEMS]:
        if not isinstance(key, (str, int, float, bool)):
            continue
        name = str(key)
        if _secret_key(name):
            out[name] = "***"
            continue
        clean = _value(value, depth)
        if clean is not _DROP:
            out[name] = clean
    return out


_DROP = object()


def _value(value: Any, depth: int) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _DROP
    if isinstance(value, str):
        return redact_secrets(value[:_MAX_STRING])
    if isinstance(value, (_dt.date, _dt.datetime, _dt.time)):
        return value.isoformat()
    if depth >= _MAX_DEPTH:
        return _DROP
    if isinstance(value, dict):
        return _mapping(value, depth + 1)
    if isinstance(value, (list, tuple)):
        items = (_value(v, depth + 1) for v in value[:_MAX_ITEMS])
        return [v for v in items if v is not _DROP]
    return _DROP  # bytes, sets, anything else a tag could produce


def string_field(front_matter: dict[str, Any], key: str) -> str | None:
    """A non-empty string value of ``key`` (stripped), else None."""
    value = front_matter.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
