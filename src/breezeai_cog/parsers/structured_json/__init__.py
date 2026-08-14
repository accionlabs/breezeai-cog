"""Structured-JSON analyzer. Emits ``type="config"``, ``language="structured-json"``
FileRecords whose ``metadata.toon`` holds a TOON serialization of the whole data/metadata
JSON document (client-agnostic; no per-record graph nodes), plus — under
``--capture-statements`` — a single ``structured_data`` statement carrying the same TOON."""

from .parser import StructuredJsonParser

PARSERS = [StructuredJsonParser()]
