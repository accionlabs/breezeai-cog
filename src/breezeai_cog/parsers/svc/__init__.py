"""WCF ``.svc`` ServiceHost parser. Exposes ``PARSERS`` for ``discover_builtin``; owns the
``.svc`` extension so ServiceHost host files are scanned instead of dropped as unsupported."""

from __future__ import annotations

from .parser import SvcHostParser

PARSERS = [SvcHostParser()]
