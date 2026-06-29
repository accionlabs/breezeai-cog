"""TypeScript/JavaScript parser. Exposes ``PARSERS`` for ``discover_builtin``."""

from __future__ import annotations

from .parser import TypeScriptParser

PARSERS = [TypeScriptParser()]
