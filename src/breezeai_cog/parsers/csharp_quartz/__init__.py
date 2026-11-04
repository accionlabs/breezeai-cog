"""Quartz.NET job detection for C#. A registered additive detector (``jobs.py``); the rules shared
with Java live in ``parsers/quartz_common.py``. It owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
