"""NestJS parser. Exposes ``PARSERS`` for ``discover_builtin``; overrides the base
TypeScript parser for `.ts`/`.tsx`/`.js` files."""

from __future__ import annotations

from .parser import NestJSParser

PARSERS = [NestJSParser()]
