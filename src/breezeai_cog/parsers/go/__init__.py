"""Go language parser. Exposes ``PARSERS`` for ``core.registry.discover_builtin``."""

from __future__ import annotations

from .parser import GoParser

PARSERS = [GoParser()]
