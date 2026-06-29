"""In-process analysis for the server ``POST /api/analyze`` path (§10).

Writes the in-memory file list to a temp dir, runs the pipeline **sequentially**
(no spawn pool), and returns a plain ``{ projectMetaData, files }`` dict — byte-faithful
to the JS ``assembleOutputFromNdjson`` output (no gzip, no ``__type`` on the meta)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from ..config import Settings
from ..core import pipeline
from ..emit.ndjson import to_line
from ..emit.sinks import MemorySink


def analyze_in_memory(
    settings: Settings, files: list[dict[str, str]], project_name: str | None = None
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ontology-") as tmp:
        root = Path(tmp)
        for f in files:
            dest = root / f["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f["content"], encoding="utf-8")

        sink = MemorySink()
        meta = pipeline.run_inprocess(root, settings, sink)

    name = project_name or "untitled-project"
    meta = meta.model_copy(update={"repositoryName": name, "repositoryPath": name})
    meta_dict = meta.model_dump(by_alias=True, exclude_none=True)
    meta_dict.pop("__type", None)  # /api/analyze meta carries no __type (unlike the NDJSON line)

    return {
        "projectMetaData": meta_dict,
        "files": [json.loads(to_line(r)) for r in sink.records],
    }
