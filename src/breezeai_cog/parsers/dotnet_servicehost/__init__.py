"""ServiceHost-style .NET endpoint-host parser (WCF ``.svc``, ASMX ``.asmx``, handler ``.ashx``). Exposes
``PARSERS`` for ``discover_builtin``; owns those extensions so the host
markup files are scanned instead of dropped as unsupported."""

from __future__ import annotations

from .parser import ServiceHostParser

PARSERS = [ServiceHostParser()]
