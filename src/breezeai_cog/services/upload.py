"""Backend upload (mirrors the JS ``uploadToGenerate`` in ``index.js``): POST the
gzipped NDJSON ontology as ``multipart/form-data`` to ``{baseurl}/code-ontology/generate``
with the ``api-key`` header. The backend route (``POST /code-ontology/generate``,
``upload.single('file')``) streams the ``.gz`` to S3 and kicks off ingestion.

Request contract (from the backend controller):
  * multipart field ``file``  — the ``.ndjson.gz`` artifact (mimetype ``application/gzip``)
  * form field  ``projectUuid`` — ``settings.uuid``
  * form field  ``name``        — the repository name (from ``projectMetaData``)
  * header      ``api-key``     — ``settings.user_api_key``

Transient failures (network errors / HTTP 5xx) get a bounded retry; 4xx is fatal.
Following the port convention (see ``notify.py``), ``llmPlatform`` is never sent — the
backend query param is optional and defaults when absent.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..errors import UploadError

_GENERATE_PATH = "/code-ontology/generate"
_STATUS_PATH = "/code-ontology"
_STATUS_TIMEOUT = 30.0
_TERMINAL_STATUSES: frozenset[str] = frozenset({"active", "error"})
_DEFAULT_POLL_INTERVAL = 60  # seconds


def upload_ontology(
    settings: Settings,
    file_path: str | Path,
    *,
    repository_name: str,
    on_attempt: Callable[[int], None] | None = None,
) -> Any:
    """POST ``file_path`` to the Breeze backend. Returns the parsed JSON response.

    The per-request timeout is ``settings.upload_timeout`` and the attempt budget is
    ``settings.upload_max_retries + 1`` (only transient failures — network / read timeout /
    HTTP 5xx — retry; a 4xx is fatal). ``on_attempt(n)`` is called at the start of each try
    so a display can surface the current attempt number.

    Raises :class:`UploadError` on missing config, a missing artifact, a 4xx
    response, or exhausted retries on transient failures.
    """
    import httpx

    base = settings.baseurl
    if not base:
        raise UploadError("baseurl is not configured (set --baseurl / BREEZE_API_URL)")
    if not settings.uuid:
        raise UploadError("uuid (projectUuid) is not configured (set --uuid)")
    if settings.user_api_key is None:
        raise UploadError("user_api_key is not configured (set --user-api-key / API_KEY)")

    path = Path(file_path)
    if not path.is_file():
        raise UploadError(f"upload artifact not found: {path}")

    url = f"{base.rstrip('/')}{_GENERATE_PATH}"
    headers = {"api-key": settings.user_api_key.get_secret_value()}
    data = {"projectUuid": settings.uuid, "name": repository_name}

    max_attempts = settings.upload_max_retries + 1
    last_error: str | None = None
    for attempt in range(1, max_attempts + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        try:
            with path.open("rb") as fh:
                files = {"file": (path.name, fh, "application/gzip")}
                resp = httpx.post(
                    url, data=data, files=files, headers=headers,
                    timeout=settings.upload_timeout,
                )
        except httpx.TransportError as exc:  # connect/read/timeout — transient
            last_error = f"network error: {exc}"
        else:
            if resp.status_code < 400:
                return resp.json() if resp.content else None
            last_error = f"HTTP {resp.status_code} - {resp.text}"
            if resp.status_code < 500:  # 4xx is fatal — do not retry
                break

        if attempt < max_attempts:
            time.sleep(attempt)  # linear backoff

    raise UploadError(f"upload failed for {path.name}: {last_error}")


def extract_ontology_id(payload: Any) -> str | None:
    """Return the ``_id`` from an upload response, or ``None`` if absent.

    Handles both a raw document ``{"_id": "..."}`` and a ``{"data": {...}}``
    wrapper (common in Strapi-style REST APIs).
    """
    if not isinstance(payload, dict):
        return None
    doc = payload.get("data", payload)
    return doc.get("_id") if isinstance(doc, dict) else None


def _extract_status(payload: Any) -> str | None:
    """Return ``fileGraphStatus`` from a list-style poll response, or ``None``."""
    if isinstance(payload, list):
        docs: list[Any] = payload
    elif isinstance(payload, dict):
        inner = payload.get("data", [])
        if isinstance(inner, list):
            docs = inner
        elif isinstance(inner, dict):
            docs = [inner]
        else:
            docs = []
    else:
        return None
    return docs[0].get("fileGraphStatus") if docs else None


def poll_ontology_status(
    settings: Settings,
    ontology_id: str,
    *,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    overall_timeout: float | None = None,
    on_waiting: Callable[[str | None], None] | None = None,
    on_response: Callable[[Any], None] | None = None,
) -> str:
    """Poll ``GET /code-ontology`` until ``fileGraphStatus`` reaches a terminal value.

    Terminal values are ``"active"`` and ``"error"``; any other value (or a
    missing field) triggers a sleep of ``poll_interval`` seconds and a retry.

    ``overall_timeout`` (seconds) caps the *total* time spent waiting for the backend
    to finish processing — past it the poll gives up with an :class:`UploadError`
    instead of looping forever. ``None`` polls indefinitely (the historical behaviour).

    ``on_response(payload)`` is called on *every* successful HTTP response
    (including the terminal one) so callers can log the raw data.
    ``on_waiting(current_status)`` is called just before each sleep.

    Returns the terminal status string.
    Raises :class:`UploadError` on HTTP / network errors or an exceeded ``overall_timeout``.
    """
    import httpx

    base = settings.baseurl
    if not base:
        raise UploadError("baseurl is not configured")
    if settings.user_api_key is None:
        raise UploadError("user_api_key is not configured")

    url = f"{base.rstrip('/')}{_STATUS_PATH}"
    headers = {"api-key": settings.user_api_key.get_secret_value()}
    params = {
        "filters[projectUuid][$eq]": settings.uuid,
        "filters[_id][$eq]": ontology_id,
        "selectedFields": "_id,fileGraphStatus",
    }

    start = time.monotonic()
    last_status: str | None = None
    while True:
        if overall_timeout is not None and time.monotonic() - start >= overall_timeout:
            raise UploadError(
                f"backend did not finish processing within {overall_timeout:.0f}s "
                f"(last status: {last_status or 'pending'})"
            )
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=_STATUS_TIMEOUT)
        except httpx.TransportError as exc:
            raise UploadError(f"status poll network error: {exc}") from exc

        if resp.status_code >= 400:
            raise UploadError(f"status poll HTTP {resp.status_code}: {resp.text}")

        payload = resp.json()
        if on_response is not None:
            on_response(payload)

        status = _extract_status(payload)
        if status in _TERMINAL_STATUSES:
            return status
        last_status = status

        if on_waiting is not None:
            on_waiting(status)
        # Don't sleep past the deadline — wake in time to bail on the next iteration.
        sleep_for = poll_interval
        if overall_timeout is not None:
            sleep_for = max(0.0, min(poll_interval, overall_timeout - (time.monotonic() - start)))
        time.sleep(sleep_for)
