"""ServiceHost-style .NET endpoint-host parser (WCF ``.svc`` + ASMX ``.asmx``). Exposes
``PARSERS`` for ``discover_builtin``; owns the ``.svc``/``.asmx`` extensions so the host
markup files are scanned instead of dropped as unsupported."""

from __future__ import annotations

from .parser import ServiceHostParser

PARSERS = [ServiceHostParser()]
