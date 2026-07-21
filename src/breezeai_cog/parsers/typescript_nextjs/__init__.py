"""Next.js App Router parser. Exposes ``PARSERS`` for ``discover_builtin``; selected
per-file over the base TypeScript parser via ``claims`` (an ``app/**/route.*`` file that
exports an HTTP-verb handler)."""

from __future__ import annotations

from .parser import NextJSParser

PARSERS = [NextJSParser()]
