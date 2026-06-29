"""Server /health + /api/analyze: contract shape, in-process analysis, 400/422 errors."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from breezeai_cog.config import Settings
from breezeai_cog.server.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings()))


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_analyze_returns_project_meta_and_files(client: TestClient) -> None:
    body = {
        "projectName": "demo",
        "files": [
            {"path": "app/main.py", "content": "def add(a, b):\n    return a + b\n"},
            {"path": "app/util.py", "content": "class Helper:\n    def run(self):\n        return 1\n"},
        ],
    }
    r = client.post("/api/analyze", json=body)
    assert r.status_code == 200
    out = r.json()
    assert set(out) == {"projectMetaData", "files"}
    meta = out["projectMetaData"]
    assert "__type" not in meta  # /api/analyze meta is bare (no NDJSON __type)
    assert meta["repositoryName"] == "demo" and meta["repositoryPath"] == "demo"
    assert meta["totalFiles"] == 2 and "python" in meta["analyzedLanguages"]
    assert len(out["files"]) == 2
    assert {f["path"] for f in out["files"]} == {"app/main.py", "app/util.py"}


def test_analyze_rejects_empty_files(client: TestClient) -> None:
    r = client.post("/api/analyze", json={"files": []})
    assert r.status_code == 400 and r.json() == {"error": '"files" must be a non-empty array'}


def test_analyze_rejects_bad_file_shape(client: TestClient) -> None:
    r = client.post("/api/analyze", json={"files": [{"path": "x.py"}]})
    assert r.status_code == 400
    assert r.json() == {"error": 'files[0] must have "path" (string) and "content" (string)'}


def test_analyze_rejects_path_traversal(client: TestClient) -> None:
    r = client.post("/api/analyze", json={"files": [{"path": "../evil.py", "content": "x = 1"}]})
    assert r.status_code == 400 and r.json() == {"error": 'files[0].path must not contain ".."'}


def test_analyze_unsupported_language_is_422(client: TestClient) -> None:
    r = client.post("/api/analyze", json={"files": [{"path": "notes.xyz", "content": "hello"}]})
    assert r.status_code == 422
    assert r.json() == {"error": "No supported languages detected in the provided files"}
