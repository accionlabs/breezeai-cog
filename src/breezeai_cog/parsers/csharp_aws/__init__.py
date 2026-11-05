"""AWS for .NET detection for C#. Registered additive detectors — Lambda handlers
(``lambda_events.py``) and SDK calls (``sdk_calls.py``); the package owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
