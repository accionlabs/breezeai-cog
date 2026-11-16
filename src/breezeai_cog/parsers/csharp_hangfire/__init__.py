"""Hangfire background-job detection for C#. A registered additive detector (``jobs.py``) — it
owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
