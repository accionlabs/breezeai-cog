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
    # Backend compatibility shim: the object-key field was renamed s3Key -> storage_key
    # with no alias and no version bump, so a backend predating that rename rejects the
    # payload outright ("s3Key should not be empty"). Send both names until every
    # deployed backend accepts storage_key, then delete these two lines.
    if "storage_key" in payload and "s3Key" not in payload:
        payload = {**payload, "s3Key": payload["storage_key"]}

    resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
    if resp.status_code >= 400:
        # raise_for_status() discards the body, which is where the backend says *why* it
        # refused. This runs in a BackgroundTask after the 200 has gone out, so the log
        # line is the only signal anyone gets.
        raise RuntimeError(f"Breeze API {resp.status_code} for {path}: {resp.text[:500]}")
    return resp.json() if resp.content else None
