"""NestJS microservice message patterns, written so a sender and its consumer match.

``@MessagePattern({ cmd: 'sum' })`` and ``client.send({cmd:"sum"}, data)`` name the same handler:
NestJS serialises an object pattern as JSON with its keys sorted, so spacing, quote style and key
order do not matter. Both sides are written that way here — ``{"cmd":"sum"}`` — so the endpoints
compare equal. A string pattern is the string. Any part that is not a literal (a runtime
variable, a computed key, a spread) leaves the pattern unknown: ``None``, never source text.

A constant inside an object or array is unknown when its value is a number's digits: the constant
index holds ``5`` and ``'5'`` alike as ``"5"``, and JSON writes them differently (``5`` / ``"5"``)."""

from __future__ import annotations

import json
import re
from typing import Any

from tree_sitter import Node

from ..treesitter import node_text
from ..typescript.imports import resolve_const_value

_UNKNOWN = object()
#: What the constant index holds for a number (``number_literal``): ``5000``, ``1.5``.
_NUMERIC_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def _value(node: Node, source: bytes, consts: dict[str, str | None], nested: bool = False) -> Any:
    t = node.type
    if t == "object":
        out: dict[str, Any] = {}
        for c in node.named_children:
            if c.type == "comment":
                continue
            if c.type == "pair":
                key = c.child_by_field_name("key")
                value = c.child_by_field_name("value")
                if key is None or value is None:
                    return _UNKNOWN
                if key.type in ("property_identifier", "identifier"):
                    name = node_text(key, source)
                elif key.type in ("string", "number"):
                    name = str(_value(key, source, consts))
                else:  # computed key [x]
                    return _UNKNOWN
                v = _value(value, source, consts, nested=True)
            elif c.type == "shorthand_property_identifier":  # { cmd } → the variable cmd
                name = node_text(c, source)
                v = _value(c, source, consts, nested=True)
            else:  # spread, method
                return _UNKNOWN
            if v is _UNKNOWN:
                return _UNKNOWN
            out[name] = v
        return out
    if t == "array":
        items = [_value(c, source, consts, nested=True)
                 for c in node.named_children if c.type != "comment"]
        return _UNKNOWN if any(i is _UNKNOWN for i in items) else items
    if t == "number":
        text = node_text(node, source)
        try:
            return int(text) if text.isdigit() else float(text)
        except ValueError:
            return _UNKNOWN
    if t in ("true", "false"):
        return t == "true"
    if t == "null":
        return None
    if t in ("string", "template_string", "identifier", "member_expression",
             "shorthand_property_identifier"):
        if t == "shorthand_property_identifier":
            resolved = consts.get(node_text(node, source))
        else:
            resolved = resolve_const_value(node, source, consts)
        if resolved is None or (nested and t not in ("string", "template_string")
                                 and _NUMERIC_RE.match(resolved)):
            return _UNKNOWN
        return resolved
    if t == "parenthesized_expression" and node.named_children:
        return _value(node.named_children[0], source, consts, nested)
    return _UNKNOWN


def message_pattern(arg: Node, source: bytes, consts: dict[str, str | None]) -> str | None:
    """The endpoint of a message pattern argument: the string for a string pattern, canonical
    JSON (keys sorted, no spaces) for an object pattern, ``None`` when any part is unknown."""
    v = _value(arg, source, consts)
    if v is _UNKNOWN or v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
