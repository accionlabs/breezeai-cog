"""Next.js route handlers and pages. Next.js is **not** a selecting parser for TypeScript: its
route handlers (``routes.py``, ``nextjs-routes``) and pages (``pages.py``, ``nextjs-pages``) are
additive detectors, found by discovery, so they are captured whatever parser owns the file."""

from __future__ import annotations

PARSERS: list[object] = []
