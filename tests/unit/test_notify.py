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


# --- retry on transient failures (BREEZEAI-520 / BREEZEAI-681) ----------------------

def _stub_sequence(monkeypatch: pytest.MonkeyPatch, responses: list) -> list[float]:
    """Each entry is an int status (canned response) or an exception to raise."""
    slept: list[float] = []
    calls = iter(responses)

    def fake_post(url, json, headers, timeout):  # noqa: A002
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, text="", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("breezeai_cog.services.notify._sleep", lambda s: slept.append(s))
    return slept


def test_retries_on_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = _stub_sequence(monkeypatch, [503, 200])

    assert post_notification(_settings(), "/p", {"storage_key": "k"}) is None
    assert slept == [1.0]


def test_retries_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    req = httpx.Request("POST", "http://backend:3004/p")
    slept = _stub_sequence(monkeypatch, [httpx.ConnectError("refused", request=req), 200])

    post_notification(_settings(), "/p", {"storage_key": "k"})
    assert slept == [1.0]


def test_read_timeout_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend may have processed a request that timed out on read; a repeat
    could start a second ingest of the same artifact."""
    req = httpx.Request("POST", "http://backend:3004/p")
    slept = _stub_sequence(monkeypatch, [httpx.ReadTimeout("slow", request=req), 200])

    with pytest.raises(httpx.ReadTimeout):
        post_notification(_settings(), "/p", {"storage_key": "k"})
    assert slept == []


def test_gives_up_after_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = _stub_sequence(monkeypatch, [503, 503, 503])

    with pytest.raises(RuntimeError, match=r"Breeze API 503 for /p.*after 3 attempts"):
        post_notification(_settings(), "/p", {"storage_key": "k"})
    assert slept == [1.0, 3.0]


def test_does_not_retry_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    slept = _stub_sequence(monkeypatch, [400])

    with pytest.raises(RuntimeError, match="Breeze API 400"):
        post_notification(_settings(), "/p", {"storage_key": "k"})
    assert slept == []
