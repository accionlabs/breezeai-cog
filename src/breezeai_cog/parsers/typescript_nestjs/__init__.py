"""NestJS parser. Exposes ``PARSERS`` for ``discover_builtin``; selected per-file over
the base TypeScript parser via ``claims`` (an ``@nestjs/`` import)."""

from __future__ import annotations

from .parser import NestJSParser

PARSERS = [NestJSParser()]
