"""In-file framework fingerprints — the FALLBACK when cross-file resolution can't name a
template's framework.

The cross-file resolver (see :mod:`.index`) is the authoritative signal — it names the
framework *and* links the component. This module is the fallback for templates the resolver
missed: it reads the markup's own binding syntax. Two separate concerns, learned from
measuring real repos:

* **Rendering framework** — what *renders* the file. Owns the single ``framework`` field.
  Only **uniquely-identifying** markers are used (validated at ~0% cross-label on real repos):
  Angular's ``(x)=``/``[x]=``/``*ng``, AngularJS ``ng-*``, Vue ``v-*`` (NOT the ``@``/``:``
  shorthand — it collides with Alpine), Aurelia ``.bind=``, Knockout ``data-bind=``, Thymeleaf
  ``th:``. Shared dialects (``{{ }}``, ``{% %}``, ``<% %>``) are deliberately excluded — they
  can't name one framework, so they stay honest-null (the resolver names those via context).
  If two rendering frameworks match, the result is ambiguous → ``None`` (absent beats wrong).

* **Behavior library** — progressive-enhancement sprinkled *on top of* whatever renders the
  page (htmx / Alpine / Stimulus). It is not the rendering framework, so it never occupies the
  ``framework`` field; it is an additive ``behaviors`` marker. A Django page with htmx is
  ``framework`` (rendered elsewhere) + ``behaviors=["htmx"]`` — not a collision.
"""

from __future__ import annotations

import re

#: Rendering frameworks — UNIQUELY-identifying markup only (own the ``framework`` field).
_RENDERING: dict[str, re.Pattern[str]] = {
    "angular": re.compile(
        r'\([a-zA-Z][\w]*\)\s*=\s*["\']|\[\(\s*\w+\s*\)\]\s*=|'
        r'\[[a-zA-Z][\w.]*\]\s*=\s*["\']|\*ng[A-Z]\w+|@(if|for|switch|defer)\s*[\(\{]'
    ),
    "angularjs": re.compile(
        r"\bng-(click|repeat|if|model|show|hide|bind|app|controller|submit|include|"
        r"init|switch|change|cloak|view|src|href|class|style|disabled|options)\b"
    ),
    "vue": re.compile(
        r"\bv-(if|for|else|else-if|show|model|bind|on|html|text|cloak|slot|pre|once)\b"
    ),
    "aurelia": re.compile(
        r'\.(bind|trigger|delegate|one-time|two-way|call)\s*=\s*["\']|\brepeat\.for\s*='
    ),
    "knockout": re.compile(r'\bdata-bind\s*=\s*["\']'),
    "thymeleaf": re.compile(r"\bth:[a-z]+\s*=|xmlns:th"),
}

#: Behavior libraries — additive enhancement, NOT the rendering framework.
#: Stimulus keys on ``data-controller`` ONLY — its unambiguous activation attribute. ``data-
#: action`` / ``data-*-target`` are deliberately excluded: they collide with Bootstrap
#: (``data-bs-target``) and generic ``data-action`` attributes (a confirmed false positive).
_BEHAVIOR: dict[str, re.Pattern[str]] = {
    "htmx": re.compile(
        r"\bhx-(get|post|put|delete|patch|trigger|target|swap|boost|on|vals|push-url|select)\b"
    ),
    "alpine": re.compile(
        r"\bx-(data|on|bind|model|for|if|show|text|html|init|ref|effect|cloak|transition)\b"
    ),
    "stimulus": re.compile(r"\bdata-controller\s*="),
}

#: Non-content regions stripped before fingerprinting: framework code shown *as an example*
#: (inside ``<pre>``/``<code>``), script/style bodies, textarea contents, and comments. A docs
#: page that merely *displays* ``*ngFor`` or ``v-if`` must not be mislabeled as using it.
#: Statement parsing still uses the full source — only the framework SNIFF is narrowed.
_NON_CONTENT = re.compile(
    r"<!--.*?-->|<(pre|code|script|style|textarea)\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)


def _content(text: str) -> str:
    """``text`` with example/code/script/comment regions blanked — see :data:`_NON_CONTENT`."""
    return _NON_CONTENT.sub(" ", text)


def detect_rendering_framework(text: str) -> str | None:
    """The rendering framework, from uniquely-identifying markup — or ``None`` when nothing
    matches or **two** frameworks match (ambiguous → honest-null). Framework code shown inside
    ``<pre>``/``<code>`` is ignored, so a docs page isn't mislabeled as using the framework."""
    body = _content(text)
    hits = [fw for fw, rx in _RENDERING.items() if rx.search(body)]
    return hits[0] if len(hits) == 1 else None


def detect_behaviors(text: str) -> list[str]:
    """Sorted behavior libraries layered on the markup (htmx / Alpine / Stimulus); ``[]`` if
    none. Independent of the rendering framework — both can be present."""
    body = _content(text)
    return sorted(fw for fw, rx in _BEHAVIOR.items() if rx.search(body))
