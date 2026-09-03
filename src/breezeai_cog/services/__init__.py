"""Application use-case layer (the DI seam) — called by the library API, CLI, and
server. Wires config + core pipeline + emit."""

from __future__ import annotations

from .analysis import AnalysisResult, AnalysisService
from .batch_upload import (
    UploadState,
    UploadTask,
    UploadTracker,
    run_batch_uploads,
)
from .upload import extract_ontology_id, poll_ontology_status, upload_ontology

__all__ = [
    "AnalysisService",
    "AnalysisResult",
    "upload_ontology",
    "extract_ontology_id",
    "poll_ontology_status",
    "UploadState",
    "UploadTask",
    "UploadTracker",
    "run_batch_uploads",
]
