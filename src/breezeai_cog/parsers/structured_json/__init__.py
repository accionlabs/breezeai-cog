"""Structured-JSON analyzer. Emits ``type="config"``, ``language="structured-json"``
FileRecords whose ``metadata.fields`` holds the full recursive key/value flatten of a
data/metadata JSON document (client-agnostic; no per-record graph nodes)."""

from .parser import StructuredJsonParser

PARSERS = [StructuredJsonParser()]
