"""Output emission: the capture id convention, NDJSON serialization, streaming
gzip, and sinks (file / S3 / HTTP / in-memory)."""

from __future__ import annotations

from .ids import class_id, disambiguate, file_id, function_id, statement_id
from .ndjson import to_line
from .sinks import FileSink

__all__ = [
    "file_id",
    "class_id",
    "function_id",
    "statement_id",
    "disambiguate",
    "to_line",
    "FileSink",
]
