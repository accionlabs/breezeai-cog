"""Schema version + JSON-Schema generation for the capture contract.

The Pydantic models in ``capture.py`` are the SOURCE OF TRUTH. The JSON Schema is
*generated on demand* from them (``export_json_schema`` / the ``schema`` CLI command)
for cross-language consumers (e.g. the Node backend) and external validation. It is
not committed — generate it when a consumer needs it.
"""

from __future__ import annotations

import json
from typing import Any

# Capture contract version (semver). Bump on schema change.
SCHEMA_VERSION = "2.0"


def export_json_schema() -> dict[str, Any]:
    """Generate the language-agnostic JSON Schema from the Pydantic models.

    Two record types: line 1 is ``projectMetaData``; every later line is a
    ``fileRecord`` (code | config).
    """
    from .capture import FileRecord, ProjectMetaData

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://breeze.ai/schemas/code-capture-coverage-schema.json",
        "title": "Code Capture NDJSON",
        "x-version": SCHEMA_VERSION,
        "x-recordTypes": {"line1": "projectMetaData", "lineN": "fileRecord (type=code | config)"},
        "oneOf": [
            ProjectMetaData.model_json_schema(by_alias=True),
            FileRecord.model_json_schema(by_alias=True),
        ],
    }


def write_json_schema(path: str) -> None:
    """Write the generated JSON Schema to ``path`` (for publishing to a consumer)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(export_json_schema(), fh, indent=2)
        fh.write("\n")
