"""Backend notification (mirrors ``call-http.js`` ``httpPost``): POST JSON to
``{BREEZE_API_URL}{path}`` with the ``api-key`` header. Used by the streaming endpoints
to tell the Breeze backend to ingest the S3 artifact. ``llmPlatform`` is **never** sent
(the one accepted deviation — no LLM in this app; the backend defaults its absence)."""

from __future__ import annotations

import logging
import time
from typing import Any

from ..config import Settings

log = logging.getLogger(__name__)

# Retry only failures where the backend provably did not act on the request: the
# connection never opened (ConnectError / ConnectTimeout) or it answered 5xx (every
# backend ingest handler fails before it starts any fire-and-forget work). A read
# timeout is NOT retried — the request may have been processed and a repeat would
# start a second ingest of the same artifact. A 4xx is the backend rejecting the
# payload; repeating it cannot help. Delays are short because this runs in a
# FastAPI BackgroundTask after the response has gone out.
NOTIFY_ATTEMPTS = 3
NOTIFY_BACKOFF_SECONDS = (1.0, 3.0)
_sleep = time.sleep  # module attribute so tests can stub it


def post_notification(settings: Settings, path: str, payload: dict[str, Any]) -> Any:
    import httpx

    base = settings.baseurl
    if not base:
        raise RuntimeError("BREEZE_API_URL / baseurl is not configured")
    url = f"{base.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json"}
    if settings.user_api_key is not None:
        headers["api-key"] = settings.user_api_key.get_secret_value()
    last_error: str = ""
    for attempt in range(1, NOTIFY_ATTEMPTS + 1):
        try:
            resp = httpx.post(url, json=payload, headers=headers, timeout=30.0)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:  # never reached the backend
            last_error = f"Breeze API unreachable for {path}: {exc}"
        else:
            if resp.status_code < 400:
                return resp.json() if resp.content else None
            # raise_for_status() discards the body, which is where the backend says
            # *why* it refused. This runs in a BackgroundTask after the 200 has gone
            # out, so the log line is the only signal anyone gets.
            last_error = f"Breeze API {resp.status_code} for {path}: {resp.text[:500]}"
            if resp.status_code < 500:
                raise RuntimeError(last_error)
        if attempt < NOTIFY_ATTEMPTS:
            delay = NOTIFY_BACKOFF_SECONDS[min(attempt - 1, len(NOTIFY_BACKOFF_SECONDS) - 1)]
            log.warning("%s (attempt %d/%d, retrying in %.0fs)", last_error, attempt,
                        NOTIFY_ATTEMPTS, delay)
            _sleep(delay)
    raise RuntimeError(f"{last_error} (after {NOTIFY_ATTEMPTS} attempts)")
