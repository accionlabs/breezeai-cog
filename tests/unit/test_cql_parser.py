"""Tests for the Cassandra CQL parser."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.cql.parser import CQLParser


def _parse(tmp_path, name: str, src: bytes):
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path)
    return CQLParser().parse_file(ctx)


SIMPLE_TABLE = b"""
CREATE TABLE users (
  id UUID,
  email TEXT,
  name TEXT,
  created_at TIMESTAMP,
  PRIMARY KEY (id)
);
"""

COMPOUND_PK_TABLE = b"""
CREATE TABLE events (
  user_id UUID,
  event_id TIMEUUID,
  payload TEXT,
  PRIMARY KEY (user_id, event_id)
);
"""

KEYSPACE_TABLE = b"""
CREATE TABLE IF NOT EXISTS myapp.orders (
  order_id UUID,
  customer_id UUID,
  amount DECIMAL,
  PRIMARY KEY ((order_id), customer_id)
);
"""


def test_simple_table_detected(tmp_path):
    rec = _parse(tmp_path, "schema.cql", SIMPLE_TABLE)
    tables = [c for c in rec.classes if c.type == "table"]
    assert len(tables) == 1


def test_table_name(tmp_path):
    rec = _parse(tmp_path, "schema.cql", SIMPLE_TABLE)
    tbl = next(c for c in rec.classes if c.type == "table")
    assert tbl.name == "users"


def test_source_is_cassandra(tmp_path):
    rec = _parse(tmp_path, "schema.cql", SIMPLE_TABLE)
    tbl = next(c for c in rec.classes if c.type == "table")
    assert getattr(tbl, "source", None) == "cassandra"


def test_columns_extracted(tmp_path):
    rec = _parse(tmp_path, "schema.cql", SIMPLE_TABLE)
    tbl = next(c for c in rec.classes if c.type == "table")
    columns = getattr(tbl, "columns", [])
    col_names = [col["name"] for col in columns]
    assert "email" in col_names
    assert "name" in col_names
    assert "created_at" in col_names


def test_partition_key_marked(tmp_path):
    rec = _parse(tmp_path, "schema.cql", SIMPLE_TABLE)
    tbl = next(c for c in rec.classes if c.type == "table")
    columns = getattr(tbl, "columns", [])
    pk_col = next((c for c in columns if c["name"] == "id"), None)
    assert pk_col is not None
    assert pk_col.get("keyType") == "PARTITION_KEY"


def test_clustering_key_marked(tmp_path):
    rec = _parse(tmp_path, "schema.cql", COMPOUND_PK_TABLE)
    tbl = next(c for c in rec.classes if c.type == "table")
    columns = getattr(tbl, "columns", [])
    ck_col = next((c for c in columns if c["name"] == "event_id"), None)
    assert ck_col is not None
    assert ck_col.get("keyType") == "CLUSTERING_KEY"


def test_if_not_exists_and_keyspace_stripped(tmp_path):
    rec = _parse(tmp_path, "orders.cql", KEYSPACE_TABLE)
    tables = [c for c in rec.classes if c.type == "table"]
    assert len(tables) == 1
    assert tables[0].name == "orders"


def test_language_is_cql(tmp_path):
    rec = _parse(tmp_path, "schema.cql", SIMPLE_TABLE)
    assert rec.language == "cql"


def test_multiple_tables(tmp_path):
    src = SIMPLE_TABLE + b"\n" + COMPOUND_PK_TABLE
    rec = _parse(tmp_path, "schema.cql", src)
    tables = [c for c in rec.classes if c.type == "table"]
    assert len(tables) == 2


def test_empty_file(tmp_path):
    rec = _parse(tmp_path, "schema.cql", b"")
    assert rec.classes == []


# ---------------------------------------------------------------------------
# Gap 7 — CQL WITH CLUSTERING ORDER BY → indexes[]
# ---------------------------------------------------------------------------

CLUSTERING_ORDER_TABLE = b"""
CREATE TABLE time_series (
  sensor_id UUID,
  ts TIMESTAMP,
  value DOUBLE,
  PRIMARY KEY (sensor_id, ts)
) WITH CLUSTERING ORDER BY (ts DESC);
"""


def test_cql_clustering_order_index(tmp_path):
    rec = _parse(tmp_path, "schema.cql", CLUSTERING_ORDER_TABLE)
    tables = [c for c in rec.classes if c.type == "table"]
    assert len(tables) == 1
    tbl = tables[0]
    indexes = getattr(tbl, "indexes", [])
    assert len(indexes) >= 1
    idx = indexes[0]
    assert idx.get("type") == "clustering_order"
    cols = idx.get("columns", [])
    assert any(c["column"] == "ts" and c["direction"] == "DESC" for c in cols)
