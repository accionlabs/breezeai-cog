"""Schema-drift gate: the committed JSON-Schema artifact must match the schema
generated from the Pydantic models. If this fails, regenerate it::

    PYTHONPATH=src python -c "from breezeai_cog.schemas import write_json_schema; \
        write_json_schema('src/breezeai_cog/schemas/code-capture.schema.json')"
"""

from __future__ import annotations

import json
from importlib.resources import files

from breezeai_cog.schemas import export_json_schema


def test_committed_schema_matches_models() -> None:
    committed = json.loads(
        files("breezeai_cog.schemas").joinpath("code-capture.schema.json").read_text("utf-8")
    )
    assert committed == export_json_schema(), (
        "Generated JSON Schema drifted from the committed artifact — regenerate it "
        "(see this module's docstring)."
    )
