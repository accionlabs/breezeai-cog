"""Joining Django URL patterns into one address.

Django matches a URL by concatenation: the root urlconf's pattern, then the included urlconf's,
each matched against what is left of the path (which has no leading ``/``). So the address is
``/`` + the parts in order::

    path("api/", include("shop.urls"))    +   path("orders/<int:pk>/", detail)
    → /api/orders/<int:pk>/

A part is ``(text, is_regex)``: ``path()`` routes are path syntax, ``re_path()`` / ``url()`` routes
regexes. A regex that is only literal text (``^about/$``) is a path. When a real regex remains,
the address is the parts joined as one regex without their anchors (``isRegex``); a converter
(``<int:pk>``) cannot be written inside a regex, so that mix has no address.
"""

from __future__ import annotations

#: One pattern of an address: (text as written, whether it is a regex).
Part = tuple[str, bool]

_META = frozenset(".^$*+?{}[]\\|()")


def _strip_anchors(regex: str) -> str:
    if regex.startswith("^"):
        regex = regex[1:]
    if regex.endswith("$") and not regex.endswith("\\$"):
        regex = regex[:-1]
    return regex


def as_path(regex: str) -> str | None:
    """The regex as path syntax when it is only literal text (``^about/$`` → ``about/``)."""
    text = _strip_anchors(regex)
    return None if any(c in _META for c in text) else text


def _escape(text: str) -> str:
    return "".join("\\" + c if c in _META else c for c in text)


def join(parts: list[Part]) -> tuple[str, bool] | None:
    """(endpoint, is_regex) of the parts in order; None when they can't be joined."""
    plain: list[Part] = []
    for text, is_regex in parts:
        literal = as_path(text) if is_regex else None
        plain.append((literal, False) if literal is not None else (text, is_regex))
    if not any(is_regex for _, is_regex in plain):
        return "/" + "".join(text for text, _ in plain), False
    if any("<" in text for text, is_regex in plain if not is_regex):
        return None  # a converter inside a regex address
    return "/" + "".join(_strip_anchors(t) if r else _escape(t) for t, r in plain), True
