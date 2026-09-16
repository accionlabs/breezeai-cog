"""Output emission: the capture id convention, NDJSON serialization, streaming
gzip, and sinks (file / S3 / HTTP / in-memory)."""

from __future__ import annotations

from .ids import (
    SeenIds,
    class_id,
    disambiguate,
    file_id,
    find_statement_by_span,
    function_id,
    register_statement_span,
    statement_id,
)
from .ndjson import to_line
from .sinks import FileSink
from .split import split_oversized_statements

__all__ = [
    "file_id",
    "class_id",
    "function_id",
    "statement_id",
    "disambiguate",
    "SeenIds",
    "find_statement_by_span",
    "register_statement_span",
    "to_line",
    "FileSink",
    "split_oversized_statements",
]
