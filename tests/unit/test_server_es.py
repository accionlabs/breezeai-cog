"""POST /api/analyze-es — ES record building, S3 streaming, notify, 202 contract.
Uses in-memory fake deps (no AWS / no live backend)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from breezeai_cog.config import Settings
from breezeai_cog.server.app import create_app
from breezeai_cog.server.deps import ServerDeps

MAPPING = json.dumps({
    "products": {
        "aliases": {"all": {"is_write_index": True}},
        "mappings": {"properties": {
            "title": {"type": "text", "analyzer": "english",
                      "fields": {"raw": {"type": "keyword", "ignore_above": 256}}},
            "owner": {"properties": {"name": {"type": "keyword"}}},
            "tags": {"type": "nested", "properties": {"label": {"type": "keyword"}}},
        }},
    }
})

SETTINGS = json.dumps({
    "products": {"settings": {"index": {
        "number_of_shards": "3", "number_of_replicas": "1",
        "analysis": {"analyzer": {"default": {"type": "standard"}}},
    }}}
})


class _Captured:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.records: list[dict] = []
        self.notifications: list[tuple[str, dict]] = []


class _FakeS3:
    def __init__(self, captured: _Captured) -> None:
        self._captured = captured
        self._lines: list[str] = []

    def write_line(self, line: str) -> None:
        self._lines.append(line)

    def close(self) -> str:
        for line in self._lines:
            self._captured.records.append(json.loads(line))
        return "ok"


@pytest.fixture
def captured() -> _Captured:
    return _Captured()


@pytest.fixture
def client(captured: _Captured) -> TestClient:
    def open_s3(key: str) -> _FakeS3:
        captured.keys.append(key)
        return _FakeS3(captured)

    def notify(path: str, payload: dict) -> None:
        captured.notifications.append((path, payload))

    deps = ServerDeps(settings=Settings(), open_s3=open_s3, notify=notify)
    return TestClient(create_app(Settings(), deps))


def test_mapping_upload(client: TestClient, captured: _Captured) -> None:
    r = client.post(
        "/api/analyze-es",
        files={"file": ("products.json", MAPPING, "application/json")},
        data={"projectUuid": "P1", "dataLakeId": "D1"},
    )
    assert r.status_code == 202
    out = r.json()
    assert out["mode"] == "mapping" and out["indexCount"] == 1 and out["fieldCount"] == 6
    assert out["s3Key"].startswith("es-ontology/P1/D1/") and out["s3Key"].endswith(".ndjson.gz")
    rec = captured.records[0]
    assert rec["__type"] == "es_index" and rec["indexName"] == "products"
    assert rec["aliases"][0] == {"name": "all", "filter": None, "isWriteIndex": True}
    assert {f["fullPath"] for f in rec["fields"]} == {
        "title", "title.raw", "owner", "owner.name", "tags", "tags.label"
    }
    assert next(f for f in rec["fields"] if f["fullPath"] == "title.raw")["isMultiField"] is True
    path, payload = captured.notifications[0]
    assert path == "/db-ontology/stream-ingest-s3"
    assert payload == {"s3Key": out["s3Key"], "projectUuid": "P1", "dataLakeId": "D1",
                       "repositoryName": "products.json"}


def test_settings_only_upload(client: TestClient, captured: _Captured) -> None:
    r = client.post(
        "/api/analyze-es",
        files={"file": ("settings.json", SETTINGS, "application/json")},
        data={"projectUuid": "P1", "dataLakeId": "D1"},
    )
    assert r.status_code == 202
    out = r.json()
    assert out["mode"] == "settings-only" and "-settings" in out["s3Key"]
    rec = captured.records[0]
    assert rec["__type"] == "es_settings" and rec["shards"] == 3 and rec["replicas"] == 1
    assert rec["defaultAnalyzer"] == "standard"


def test_requires_file(client: TestClient) -> None:
    r = client.post("/api/analyze-es", data={"projectUuid": "P1", "dataLakeId": "D1"})
    assert r.status_code == 400 and r.json() == {"error": "At least one multipart 'file' is required"}


def test_requires_project_uuid(client: TestClient) -> None:
    r = client.post(
        "/api/analyze-es",
        files={"file": ("m.json", MAPPING, "application/json")},
        data={"dataLakeId": "D1"},
    )
    assert r.status_code == 400 and r.json() == {"error": "projectUuid is required"}


def test_unknown_file_is_422(client: TestClient) -> None:
    r = client.post(
        "/api/analyze-es",
        files={"file": ("x.json", json.dumps({"foo": "bar"}), "application/json")},
        data={"projectUuid": "P1", "dataLakeId": "D1"},
    )
    assert r.status_code == 422
    assert "Could not determine" in r.json()["error"]
