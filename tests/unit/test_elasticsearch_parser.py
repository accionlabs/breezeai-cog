"""Tests for the Elasticsearch JSON index mapping parser."""

from __future__ import annotations

import json

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.elasticsearch.parser import ElasticsearchParser


def _parse(tmp_path, name: str, src: bytes):
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(path=name, abs_path=p, source=src, repo_root=tmp_path)
    return ElasticsearchParser().parse_file(ctx)


ES_MAPPING = json.dumps({
    "mappings": {
        "properties": {
            "email": {"type": "keyword"},
            "name": {"type": "text", "analyzer": "standard"},
            "bio": {"type": "text", "analyzer": "english"},
            "createdAt": {"type": "date"},
        }
    },
    "settings": {"number_of_shards": 1, "number_of_replicas": 1}
}).encode()


def test_claims_es_json(tmp_path):
    parser = ElasticsearchParser()
    p = tmp_path / "users.json"
    p.write_bytes(ES_MAPPING)
    assert parser.claims("users.json", ES_MAPPING) is True


def test_does_not_claim_plain_json(tmp_path):
    parser = ElasticsearchParser()
    src = b'{"name": "test", "value": 123}'
    assert parser.claims("plain.json", src) is False


def test_index_class_emitted(tmp_path):
    rec = _parse(tmp_path, "users.json", ES_MAPPING)
    classes = [c for c in rec.classes if c.type == "index_mapping"]
    assert len(classes) == 1


def test_index_source(tmp_path):
    rec = _parse(tmp_path, "users.json", ES_MAPPING)
    cls = next(c for c in rec.classes if c.type == "index_mapping")
    assert getattr(cls, "source", None) == "elasticsearch"


def test_index_name_from_filename(tmp_path):
    rec = _parse(tmp_path, "users.json", ES_MAPPING)
    cls = next(c for c in rec.classes if c.type == "index_mapping")
    assert cls.name == "users"


def test_columns_extracted(tmp_path):
    rec = _parse(tmp_path, "users.json", ES_MAPPING)
    cls = next(c for c in rec.classes if c.type == "index_mapping")
    columns = getattr(cls, "columns", [])
    col_names = [col["name"] for col in columns]
    assert "email" in col_names
    assert "name" in col_names
    assert "createdAt" in col_names


def test_keyword_datatype(tmp_path):
    rec = _parse(tmp_path, "users.json", ES_MAPPING)
    cls = next(c for c in rec.classes if c.type == "index_mapping")
    columns = getattr(cls, "columns", [])
    email_col = next(c for c in columns if c["name"] == "email")
    assert email_col["dataType"] == "keyword"


def test_analyzer_extracted(tmp_path):
    rec = _parse(tmp_path, "users.json", ES_MAPPING)
    cls = next(c for c in rec.classes if c.type == "index_mapping")
    columns = getattr(cls, "columns", [])
    name_col = next(c for c in columns if c["name"] == "name")
    assert name_col.get("analyzer") == "standard"


def test_language_is_elasticsearch(tmp_path):
    rec = _parse(tmp_path, "users.json", ES_MAPPING)
    assert rec.language == "elasticsearch"
