"""LoopBack parser. Exposes ``PARSERS`` for ``discover_builtin``; selected per-file over
the base TypeScript parser via ``claims`` (a ``@loopback/`` import)."""

from __future__ import annotations

from .parser import LoopBackParser

PARSERS = [LoopBackParser()]
