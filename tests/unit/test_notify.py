"""`services/notify.py`: storage-key payloads and error-body surfacing."""

from __future__ import annotations

import httpx
import pytest

from breezeai_cog.config import Settings
from breezeai_cog.services.notify import post_notification


def _settings() -> Settings:
    return Settings(baseurl="http://backend:3004")


def _stub(monkeypatch: pytest.MonkeyPatch, status: int, body: str) -> list[dict]:
    """Capture the posted JSON and return a canned response."""
    sent: list[dict] = []

    def fake_post(url, json, headers, timeout):  # noqa: A002 - httpx kwarg name
        sent.append(json)
        return httpx.Response(status, text=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def test_storage_key_is_sent_without_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub(monkeypatch, 200, "")

    post_notification(_settings(), "/code-ontology/stream-ingest", {"storage_key": "a/b.gz"})

    assert sent[0] == {"storage_key": "a/b.gz"}


def test_payload_without_storage_key_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _stub(monkeypatch, 200, "")

    post_notification(_settings(), "/p", {"projectUuid": "u"})

    assert sent[0] == {"projectUuid": "u"}


def test_error_body_is_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 400 body says why; raise_for_status() used to throw it away."""
    _stub(monkeypatch, 400, '{"message":"storage_key should not be empty"}')

    with pytest.raises(RuntimeError, match="storage_key should not be empty"):
        post_notification(_settings(), "/p", {"storage_key": "a/b.gz"})
