"""React parser. Exposes ``PARSERS`` for ``discover_builtin``; selected per-file over
the base TypeScript parser via ``claims`` (a ``react-router`` import)."""

from __future__ import annotations

from .parser import ReactParser

PARSERS = [ReactParser()]
