"""Application use-case layer (the DI seam) — called by the library API, CLI, and
server. Wires config + core pipeline + emit."""

from __future__ import annotations

from .analysis import AnalysisResult, AnalysisService
from .upload import upload_ontology

__all__ = ["AnalysisService", "AnalysisResult", "upload_ontology"]
