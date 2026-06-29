"""POST /api/analyze-sql — DDL parse, db-ontology S3 streaming, notify, 202 contract."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from breezeai_cog.config import Settings
from breezeai_cog.server.app import create_app
from breezeai_cog.server.deps import ServerDeps

DDL = """
CREATE TABLE hr.employees (
  emp_id INT NOT NULL,
  name VARCHAR(200) NOT NULL,
  dept_id INT,
  CONSTRAINT pk_emp PRIMARY KEY (emp_id),
  CONSTRAINT fk_dept FOREIGN KEY (dept_id) REFERENCES hr.departments (dept_id)
);
CREATE VIEW active AS SELECT emp_id FROM hr.employees;
CREATE INDEX idx_dept ON hr.employees (dept_id);
"""


class _Captured:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.records: list[dict] = []
        self.notifications: list[tuple[str, dict]] = []


class _FakeS3:
    def __init__(self, c: _Captured) -> None:
        self._c, self._lines = c, []

    def write_line(self, line: str) -> None:
        self._lines.append(line)

    def close(self) -> str:
        self._c.records.extend(json.loads(x) for x in self._lines)
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


def test_analyze_sql(client: TestClient, captured: _Captured) -> None:
    r = client.post(
        "/api/analyze-sql",
        files={"file": ("schema_pg.sql", DDL, "application/sql")},
        data={"projectUuid": "P1", "dataLakeId": "D1"},
    )
    assert r.status_code == 202
    out = r.json()
    assert out["fileName"] == "schema_pg.sql" and out["dialect"] == "postgresql"
    assert out["tableCount"] == 1 and out["viewCount"] == 1 and out["indexCount"] == 1
    assert out["s3Key"].startswith("db-ontology/P1/D1/") and out["s3Key"].endswith(".ndjson.gz")
    rec = captured.records[0]
    assert rec["__type"] == "ddl" and rec["language"] == "sql"
    assert rec["tables"][0]["name"] == "employees" and rec["tables"][0]["hasPrimaryKey"] is True
    path, payload = captured.notifications[0]
    assert path == "/db-ontology/stream-ingest-s3"
    assert payload["s3Key"] == out["s3Key"] and payload["repositoryName"] == "schema_pg.sql"


def test_requires_file(client: TestClient) -> None:
    r = client.post("/api/analyze-sql", data={"projectUuid": "P1", "dataLakeId": "D1"})
    assert r.status_code == 400 and r.json() == {"error": "Multipart 'file' field is required"}


def test_requires_data_lake_id(client: TestClient) -> None:
    r = client.post(
        "/api/analyze-sql",
        files={"file": ("a.sql", DDL, "application/sql")},
        data={"projectUuid": "P1"},
    )
    assert r.status_code == 400 and r.json() == {"error": "dataLakeId is required"}


def test_no_ddl_objects_is_422(client: TestClient) -> None:
    r = client.post(
        "/api/analyze-sql",
        files={"file": ("empty.sql", "SELECT 1;", "application/sql")},
        data={"projectUuid": "P1", "dataLakeId": "D1"},
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "No DDL objects could be extracted from the SQL file" and "dialect" in body
