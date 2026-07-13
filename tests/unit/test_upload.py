"""Tests for the backend upload client (``services.upload.upload_ontology``): the
multipart contract, the ``api-key`` header, bounded retry on transient failures, and
the fatal-vs-retryable status split. ``httpx.post`` is faked — no network."""

from __future__ import annotations

import gzip

import httpx
import pytest

from breezeai_cog.config import Settings
from breezeai_cog.errors import UploadError
from breezeai_cog.services.upload import upload_ontology


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

    result = upload_ontology(_settings(), artifact, repository_name="r")
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
        upload_ontology(_settings(), artifact, repository_name="r")
    assert calls["n"] == 3  # bounded retry
    assert "network error" in str(exc.value)


def test_upload_missing_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("should not POST"))
    with pytest.raises(UploadError, match="artifact not found"):
        upload_ontology(_settings(), tmp_path / "nope.gz", repository_name="r")


def test_upload_missing_config(artifact, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("should not POST"))
    s = Settings(_env_file=None)  # upload disabled, no baseurl/uuid/key
    with pytest.raises(UploadError):
        upload_ontology(s, artifact, repository_name="r")
