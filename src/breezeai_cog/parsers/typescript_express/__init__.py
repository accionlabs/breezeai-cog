"""Express route detection. Express is **not** a selecting parser: because it is a
substrate other TS frameworks are built on (NestJS) or mount (Angular SSR), route
detection runs **additively** in ``TypeScriptParser.extract`` (see ``routes.detect_express``),
so Express routes are captured whatever parser owns the file. This package therefore
exposes no parser — ``PARSERS`` is empty — and ``__init__`` intentionally does not import
``parser`` so the base parser can import ``routes`` without a circular import."""

from __future__ import annotations

from ..base import LanguageParser

PARSERS: list[LanguageParser] = []
