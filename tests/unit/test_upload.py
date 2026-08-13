"""Tests for the backend upload client: upload_ontology (multipart contract, bounded
retry, fatal-vs-retryable split) and poll_ontology_status (status polling contract).
``httpx.post`` / ``httpx.get`` are faked — no network."""

from __future__ import annotations

import gzip
from typing import Any

import httpx
import pytest

from breezeai_cog.config import Settings
from breezeai_cog.errors import UploadError
from breezeai_cog.services.upload import extract_ontology_id, poll_ontology_status, upload_ontology


def _settings(**kwargs) -> Settings:
    base = dict(baseurl="https://api.example.com", uuid="proj-uuid", user_api_key="secret-key")
    base.update(kwargs)
    return Settings(_env_file=None, upload=True, **base)


@pytest.fixture
def artifact(tmp_path):
    p = tmp_path / "myrepo-project-analysis.ndjson.gz"
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write('{"projectMetaData": {}}\n')
    return p


class _Resp:
    def __init__(self, status_code: int, body: bytes = b'{"ok": true}') -> None:
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8")

    def json(self):
        import json

        return json.loads(self.content)


def test_upload_success_sends_multipart_and_api_key(artifact, monkeypatch):
    seen = {}

    def fake_post(url, *, data, files, headers, timeout):
        seen["url"] = url
        seen["data"] = data
        seen["headers"] = headers
        seen["file_field"] = files["file"]
        return _Resp(201)

    monkeypatch.setattr(httpx, "post", fake_post)

    result = upload_ontology(_settings(), artifact, repository_name="myrepo")

    assert result == {"ok": True}
    assert seen["url"] == "https://api.example.com/code-ontology/generate"
    assert seen["data"] == {"projectUuid": "proj-uuid", "name": "myrepo"}
    assert seen["headers"]["api-key"] == "secret-key"
    filename, _fh, mimetype = seen["file_field"]
    assert filename == artifact.name
    assert mimetype == "application/gzip"


def test_upload_strips_trailing_slash_from_baseurl(artifact, monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        return _Resp(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    upload_ontology(_settings(baseurl="https://api.example.com/"), artifact, repository_name="r")
    assert seen["url"] == "https://api.example.com/code-ontology/generate"


def test_upload_4xx_is_fatal_no_retry(artifact, monkeypatch):
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        return _Resp(400, b'{"message": "bad request"}')

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(UploadError) as exc:
        upload_ontology(_settings(), artifact, repository_name="r")
    assert calls["n"] == 1  # not retried
    assert "HTTP 400" in str(exc.value)


def test_upload_retries_5xx_then_succeeds(artifact, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        return _Resp(200) if calls["n"] == 3 else _Resp(503)

    monkeypatch.setattr(httpx, "post", fake_post)

    # 2 retries → 3 total attempts, succeeding on the third.
    result = upload_ontology(_settings(upload_max_retries=2), artifact, repository_name="r")
    assert result == {"ok": True}
    assert calls["n"] == 3


def test_upload_retries_network_error_then_exhausts(artifact, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(UploadError) as exc:
        upload_ontology(_settings(upload_max_retries=2), artifact, repository_name="r")
    assert calls["n"] == 3  # bounded retry (2 retries + 1)
    assert "network error" in str(exc.value)


def test_upload_default_retries_is_one_attempt_plus_one(artifact, monkeypatch):
    """Default upload_max_retries=1 → 2 total attempts."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(UploadError):
        upload_ontology(_settings(), artifact, repository_name="r")
    assert calls["n"] == 2


def test_upload_uses_configured_timeout_and_reports_attempts(artifact, monkeypatch):
    seen: dict[str, object] = {}
    attempts: list[int] = []

    def fake_post(url, *, data, files, headers, timeout):
        seen["timeout"] = timeout
        return _Resp(201)

    monkeypatch.setattr(httpx, "post", fake_post)
    upload_ontology(
        _settings(upload_timeout=42.0), artifact,
        repository_name="r", on_attempt=attempts.append,
    )
    assert seen["timeout"] == 42.0
    assert attempts == [1]  # one attempt on immediate success


def test_upload_missing_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("should not POST"))
    with pytest.raises(UploadError, match="artifact not found"):
        upload_ontology(_settings(), tmp_path / "nope.gz", repository_name="r")


def test_upload_missing_config(artifact, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("should not POST"))
    s = Settings(_env_file=None)  # upload disabled, no baseurl/uuid/key
    with pytest.raises(UploadError):
        upload_ontology(s, artifact, repository_name="r")


# ---------------------------------------------------------------------------
# extract_ontology_id
# ---------------------------------------------------------------------------

def test_extract_id_from_flat_dict():
    assert extract_ontology_id({"_id": "abc123", "name": "x"}) == "abc123"


def test_extract_id_from_data_wrapped_dict():
    assert extract_ontology_id({"data": {"_id": "abc123"}}) == "abc123"


def test_extract_id_missing_returns_none():
    assert extract_ontology_id({"name": "x"}) is None


def test_extract_id_none_payload_returns_none():
    assert extract_ontology_id(None) is None


# ---------------------------------------------------------------------------
# poll_ontology_status
# ---------------------------------------------------------------------------

def _get_settings():
    return _settings()


def test_poll_returns_active_immediately(monkeypatch):
    def fake_get(url, *, params, headers, timeout):
        assert params["filters[_id][$eq]"] == "oid-1"
        assert params["selectedFields"] == "_id,fileGraphStatus"
        return _Resp(200, b'[{"_id": "oid-1", "fileGraphStatus": "active"}]')

    monkeypatch.setattr(httpx, "get", fake_get)

    status = poll_ontology_status(_get_settings(), "oid-1")
    assert status == "active"


def test_poll_terminal_error_status(monkeypatch):
    def fake_get(url, *, params, headers, timeout):
        return _Resp(200, b'[{"_id": "x", "fileGraphStatus": "error"}]')

    monkeypatch.setattr(httpx, "get", fake_get)
    assert poll_ontology_status(_get_settings(), "x") == "error"


def test_poll_waits_until_terminal(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    responses = [
        b'[{"_id": "oid-1", "fileGraphStatus": "processing"}]',
        b'[{"_id": "oid-1", "fileGraphStatus": "processing"}]',
        b'[{"_id": "oid-1", "fileGraphStatus": "active"}]',
    ]
    calls: list[int] = []

    def fake_get(url, *, params, headers, timeout):
        idx = len(calls)
        calls.append(idx)
        return _Resp(200, responses[idx])

    monkeypatch.setattr(httpx, "get", fake_get)
    waiting: list[str | None] = []

    status = poll_ontology_status(
        _get_settings(), "oid-1", poll_interval=0, on_waiting=waiting.append
    )
    assert status == "active"
    assert len(calls) == 3
    assert waiting == ["processing", "processing"]  # called before each sleep, not on terminal


def test_poll_data_wrapped_response(monkeypatch):
    def fake_get(url, *, params, headers, timeout):
        return _Resp(200, b'{"data": [{"_id": "x", "fileGraphStatus": "active"}], "meta": {}}')

    monkeypatch.setattr(httpx, "get", fake_get)
    assert poll_ontology_status(_get_settings(), "x") == "active"


def test_poll_http_error_raises(monkeypatch):
    def fake_get(url, *, params, headers, timeout):
        return _Resp(403, b'{"message": "forbidden"}')

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(UploadError, match="HTTP 403"):
        poll_ontology_status(_get_settings(), "x")


def test_poll_network_error_raises(monkeypatch):
    def fake_get(url, *, params, headers, timeout):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", fake_get)
    with pytest.raises(UploadError, match="network error"):
        poll_ontology_status(_get_settings(), "x")


def test_poll_on_response_called_for_every_response_including_terminal(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    responses = [
        b'[{"_id": "x", "fileGraphStatus": "processing"}]',
        b'[{"_id": "x", "fileGraphStatus": "active"}]',
    ]
    calls: list[int] = []

    def fake_get(url, *, params, headers, timeout):
        idx = len(calls)
        calls.append(idx)
        return _Resp(200, responses[idx])

    monkeypatch.setattr(httpx, "get", fake_get)
    received: list[Any] = []

    poll_ontology_status(_get_settings(), "x", poll_interval=0, on_response=received.append)
    # on_response is called for every response, including the terminal one
    assert len(received) == 2
    assert received[0][0]["fileGraphStatus"] == "processing"
    assert received[1][0]["fileGraphStatus"] == "active"


def test_poll_overall_timeout_gives_up(monkeypatch):
    """A never-terminal status bails with UploadError once overall_timeout elapses."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def fake_get(url, *, params, headers, timeout):
        calls["n"] += 1
        return _Resp(200, b'[{"_id": "x", "fileGraphStatus": "processing"}]')

    # monotonic advances 1s per read so the 2s budget is exceeded after a couple of polls
    ticks = iter([0.0, 0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    monkeypatch.setattr("time.monotonic", lambda: next(ticks))
    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(UploadError, match="did not finish processing"):
        poll_ontology_status(_get_settings(), "x", poll_interval=0, overall_timeout=2)


def test_poll_strips_trailing_slash_from_baseurl(monkeypatch):
    seen: dict[str, str] = {}

    def fake_get(url, *, params, headers, timeout):
        seen["url"] = url
        return _Resp(200, b'[{"_id": "x", "fileGraphStatus": "active"}]')

    monkeypatch.setattr(httpx, "get", fake_get)
    poll_ontology_status(_settings(baseurl="https://api.example.com/"), "x")
    assert seen["url"] == "https://api.example.com/code-ontology"
