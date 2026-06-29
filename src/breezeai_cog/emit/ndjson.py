"""NDJSON serialization of capture records.

One record per line: ``model_dump_json(by_alias=True, exclude_none=True)`` so the
``__type`` discriminator emits and route-/db-only fields (``None``) stay absent.
UTF-8, ``\\n``-delimited, no pretty-print.
"""

from __future__ import annotations

from pydantic import BaseModel


def to_line(record: BaseModel) -> str:
    """Serialize one record to a single NDJSON line (including the trailing newline)."""
    return record.model_dump_json(by_alias=True, exclude_none=True) + "\n"
