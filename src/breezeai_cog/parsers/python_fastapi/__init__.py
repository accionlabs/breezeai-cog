"""FastAPI parser. Exposes ``PARSERS`` for ``core.registry.discover_builtin``;
overrides the base Python parser for `.py`."""

from __future__ import annotations

from .parser import FastAPIParser

PARSERS = [FastAPIParser()]
