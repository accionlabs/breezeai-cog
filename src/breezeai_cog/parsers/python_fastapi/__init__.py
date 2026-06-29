"""FastAPI parser. Exposes ``PARSERS`` for ``discover_builtin``; selected per-file
over the base Python parser via ``claims`` (a FastAPI import)."""

from __future__ import annotations

from .parser import FastAPIParser

PARSERS = [FastAPIParser()]
