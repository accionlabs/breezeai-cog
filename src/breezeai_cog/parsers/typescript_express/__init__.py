"""Express parser. Exposes ``PARSERS`` for ``discover_builtin``; selected per-file over
the base TypeScript parser via ``claims`` (an ``express`` import)."""

from __future__ import annotations

from .parser import ExpressParser

PARSERS = [ExpressParser()]
