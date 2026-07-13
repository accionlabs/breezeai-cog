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
from typing import Any

from ..config import Settings
from ..errors import UploadError

_GENERATE_PATH = "/code-ontology/generate"
_MAX_ATTEMPTS = 3
_TIMEOUT = 300.0  # seconds; large artifacts stream over a single request


def upload_ontology(
    settings: Settings, file_path: str | Path, *, repository_name: str
) -> Any:
    """POST ``file_path`` to the Breeze backend. Returns the parsed JSON response.

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

    last_error: str | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            with path.open("rb") as fh:
                files = {"file": (path.name, fh, "application/gzip")}
                resp = httpx.post(
                    url, data=data, files=files, headers=headers, timeout=_TIMEOUT
                )
        except httpx.TransportError as exc:  # connect/read/timeout — transient
            last_error = f"network error: {exc}"
        else:
            if resp.status_code < 400:
                return resp.json() if resp.content else None
            last_error = f"HTTP {resp.status_code} - {resp.text}"
            if resp.status_code < 500:  # 4xx is fatal — do not retry
                break

        if attempt < _MAX_ATTEMPTS:
            time.sleep(attempt)  # linear backoff

    raise UploadError(f"upload failed for {path.name}: {last_error}")
