"""FastAPI service (optional extra ``[server]``). Mirrors the JS API."""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
