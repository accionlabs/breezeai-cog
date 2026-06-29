"""Backend notification (mirrors ``call-http.js`` ``httpPost``): POST JSON to
``{BREEZE_API_URL}{path}`` with the ``api-key`` header. Used by the streaming endpoints
to tell the Breeze backend to ingest the S3 artifact. ``llmPlatform`` is **never** sent
(the one accepted deviation — no LLM in this app; the backend defaults its absence)."""

from __future__ import annotations

from typing import Any

from ..config import Settings


def post_notification(settings: Settings, path: str, payload: dict[str, Any]) -> Any:
    import httpx

    base = settings.baseurl
    if not base:
        raise RuntimeError("BREEZE_API_URL / baseurl is not configured")
    url = f"{base.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json"}
    if settings.user_api_key is not None:
        headers["api-key"] = settings.user_api_key.get_secret_value()
    resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
    resp.raise_for_status()
    return resp.json() if resp.content else None
