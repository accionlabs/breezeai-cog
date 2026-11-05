"""Quartz job detection for Java. A registered additive detector (``jobs.py``); the rules shared
with C# live in ``parsers/quartz_common.py``. It owns no file."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
