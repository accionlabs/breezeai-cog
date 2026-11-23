"""JSON the way the tools that read config files accept it.

Strict JSON first. When it fails, one pass over the text, outside string literals, turns ``//``
and ``/* */`` comments into spaces and drops a trailing comma before ``}`` / ``]`` (JSON with
comments, as ``tsconfig.json``, VS Code settings or a Vert.x ``mod.json`` are written); the
result is then read with raw control characters (a tab or line break) allowed inside strings.

Comments become spaces of the same length, line breaks kept, so a parse error after the pass
still names the true line and column of the fault. Nothing beyond JSONC is accepted: single
quotes, unquoted keys, ``NaN`` and the other JSON5 forms stay errors.
"""

from __future__ import annotations

import json
from typing import Any

#: The text had raw control characters inside a string, and nothing else to normalize.
LENIENT = "lenient"
#: The text had comments or trailing commas (and possibly raw control characters too).
JSONC = "jsonc"

_OPENERS = frozenset("[{,")


def normalize_jsonc(text: str) -> tuple[str, bool]:
    """``(text, changed)``: comments outside strings replaced by spaces (line breaks kept) and
    each trailing comma before ``}`` / ``]`` by a space. The length never changes, so offsets
    in the result are offsets in ``text``. A comma right after ``[`` / ``{`` / ``,`` is left
    for the parser to reject. Raises ``json.JSONDecodeError`` for an unterminated
    ``/*`` comment rather than reading the rest of the file as one."""
    chars = list(text)
    n = len(text)
    changed = False
    last = before_last = -1  # the last two significant characters outside comments
    i = 0
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            before_last, last = last, min(j, n - 1)
            i = j + 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] in "/*":
            if text[i + 1] == "/":
                end = text.find("\n", i)
                end = n if end == -1 else end
            else:
                close = text.find("*/", i + 2)
                if close == -1:
                    raise json.JSONDecodeError("Unterminated comment", text, i)
                end = close + 2
            for p in range(i, end):
                if chars[p] not in "\r\n":
                    chars[p] = " "
            changed = True
            i = end
            continue
        if (
            ch in "}]"
            and last >= 0
            and text[last] == ","
            and (before_last < 0 or text[before_last] not in _OPENERS)
        ):
            chars[last] = " "
            changed = True
        if not ch.isspace():
            before_last, last = last, i
        i += 1
    return ("".join(chars), True) if changed else (text, False)


def load_json(text: str) -> tuple[Any, str | None]:
    """``(data, mode)``: ``mode`` is None for strict JSON, :data:`JSONC` when comments or
    trailing commas were normalized, else :data:`LENIENT` (raw control characters in a string).
    Values are kept exactly as read (a raw tab stays a tab). A file that still fails raises the
    error of the normalized read, whose position is the true one in ``text``."""
    try:
        return json.loads(text), None
    except json.JSONDecodeError:
        pass
    normalized, changed = normalize_jsonc(text)
    return json.loads(normalized, strict=False), JSONC if changed else LENIENT


def load_json_object(text: str) -> dict[str, Any] | None:
    """A JSON / JSONC document that is an object, else None (unreadable or another type)."""
    try:
        value, _ = load_json(text)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None
