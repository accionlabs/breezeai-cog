"""Kafka detection for Go. A registered additive detector — it owns no file; typed values are
tracked by the shared ``parsers/go/values.py``."""

from ..base import BaseParser

PARSERS: list[BaseParser] = []
