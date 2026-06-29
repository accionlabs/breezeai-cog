"""Elasticsearch mapping/settings analyzer — port of ``elasticsearch/build-records.js``.

``build_es_records(uploads)`` content-sniffs each upload, parses mapping files into
``es_index`` records (one per index, fields flattened) and settings into ``es_settings``
(or merges settings into the mapping records), and returns the same envelope the JS
endpoint reports. Records are plain dicts (NDJSON → S3), nulls preserved."""

from __future__ import annotations

import json
from typing import Any


class BuildError(Exception):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def detect_es_kind(text: str) -> str:
    """'mapping' | 'setting' | 'unknown' (first match wins, mirroring detect-es-kind.js)."""
    try:
        parsed = json.loads(text)
    except Exception:
        return "unknown"
    if isinstance(parsed, str):  # double-encoded JSON → settings
        return "setting"
    if not isinstance(parsed, dict):
        return "unknown"
    if any(isinstance(v, dict) and "mappings" in v for v in parsed.values()):
        return "mapping"
    if any(isinstance(v, dict) and "settings" in v for v in parsed.values()):
        return "setting"
    return "unknown"


def _field(name: str, defn: dict, parent_full: str | None, *, is_multi: bool = False) -> dict:
    full = f"{parent_full}.{name}" if parent_full else name
    typ = defn.get("type") or ("object" if "properties" in defn else None)
    copy_to = defn.get("copy_to")
    copy_to = [copy_to] if isinstance(copy_to, str) else list(copy_to or [])
    field = {
        "name": name,
        "fullPath": full,
        "parentPath": parent_full,
        "type": typ,
        "analyzer": defn.get("analyzer"),
        "searchAnalyzer": defn.get("search_analyzer"),
        "format": defn.get("format"),
        "copyTo": copy_to,
        "index": defn.get("index") is not False,
        "docValues": defn.get("doc_values") is not False,
        "isNested": typ == "nested",
        "isObject": typ == "object",
        "isMultiField": is_multi,
    }
    if is_multi:
        field["ignoreAbove"] = defn.get("ignore_above")
    return field


def _flatten(properties: dict | None, parent_full: str | None = None) -> list[dict]:
    out: list[dict] = []
    for name, defn in (properties or {}).items():
        if not isinstance(defn, dict):
            continue
        out.append(_field(name, defn, parent_full))
        full = f"{parent_full}.{name}" if parent_full else name
        if isinstance(defn.get("properties"), dict):
            out.extend(_flatten(defn["properties"], full))
        if isinstance(defn.get("fields"), dict):
            for sub, sub_def in defn["fields"].items():
                if isinstance(sub_def, dict):
                    out.append(_field(sub, sub_def, full, is_multi=True))
    return out


def _aliases(aliases_obj: dict | None) -> list[dict]:
    out: list[dict] = []
    for name, defn in (aliases_obj or {}).items():
        defn = defn or {}
        filt = defn.get("filter")
        out.append({
            "name": name,
            "filter": json.dumps(filt) if filt is not None else None,
            "isWriteIndex": bool(defn.get("is_write_index")),
        })
    return out


def _parse_mapping(text: str, filepath: str) -> list[dict]:
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise BuildError(f"[es/mapping] {filepath}: expected an object keyed by index name")
    indices = []
    for index_name, body in obj.items():
        if not isinstance(body, dict):
            continue
        mappings = body.get("mappings")
        props = mappings.get("properties", {}) if isinstance(mappings, dict) else {}
        indices.append({
            "indexName": index_name,
            "sourcePath": filepath,
            "aliases": _aliases(body.get("aliases")),
            "fields": _flatten(props),
        })
    return indices


def _parse_settings(text: str, filepath: str) -> dict[str, dict]:
    obj = json.loads(text)
    if isinstance(obj, str):  # double-encoded
        obj = json.loads(obj)
    if not isinstance(obj, dict):
        raise BuildError(f"[es/setting] {filepath}: expected an object keyed by index name")
    result: dict[str, dict] = {}
    for index_name, body in obj.items():
        if not isinstance(body, dict):
            continue
        settings = body.get("settings")
        idx = settings.get("index") if isinstance(settings, dict) else None
        if not isinstance(idx, dict):
            idx = body.get("index") if isinstance(body.get("index"), dict) else None
        if idx is None:
            continue
        analysis = idx.get("analysis") if isinstance(idx.get("analysis"), dict) else {}
        default = (analysis.get("analyzer") or {}).get("default") or {}
        result[index_name] = {
            "shards": _to_int(idx.get("number_of_shards")),
            "replicas": _to_int(idx.get("number_of_replicas")),
            "defaultAnalyzer": default.get("type") if isinstance(default, dict) else None,
            "sourcePath": filepath,
        }
    return result


def build_es_records(uploads: list[dict[str, Any]]) -> dict[str, Any]:
    if not uploads:
        raise BuildError("At least one ES JSON file is required", 400)

    mapping_files, setting_files = [], []
    for u in uploads:
        kind = detect_es_kind(u["text"])
        if kind == "mapping":
            mapping_files.append(u)
        elif kind == "setting":
            setting_files.append(u)
        else:
            raise BuildError(f"Could not determine whether {u['name']} is a mapping or setting JSON")

    if not mapping_files and not setting_files:
        raise BuildError("No valid Elasticsearch mapping or setting JSON could be classified")

    if mapping_files:
        indices: list[dict] = []
        for u in mapping_files:
            indices.extend(_parse_mapping(u["text"], u["name"]))
        if not indices:
            raise BuildError("No Elasticsearch indices could be extracted from the uploaded file(s)")
        settings_by_index: dict[str, dict] = {}
        for u in setting_files:
            try:
                settings_by_index.update(_parse_settings(u["text"], u["name"]))
            except BuildError:
                continue  # non-fatal when mappings are present
        records = [
            {
                "__type": "es_index",
                "path": idx["sourcePath"],
                "indexName": idx["indexName"],
                "shards": settings_by_index.get(idx["indexName"], {}).get("shards"),
                "replicas": settings_by_index.get(idx["indexName"], {}).get("replicas"),
                "defaultAnalyzer": settings_by_index.get(idx["indexName"], {}).get("defaultAnalyzer"),
                "aliases": idx["aliases"],
                "fields": idx["fields"],
            }
            for idx in indices
        ]
        return {
            "records": records,
            "kind": "mapping",
            "mapping": {"name": mapping_files[0]["name"]},
            "setting": {"name": setting_files[0]["name"]} if setting_files else None,
            "mappings": [{"name": u["name"]} for u in mapping_files],
            "settings": [{"name": u["name"]} for u in setting_files],
            "indexCount": len(indices),
            "fieldCount": sum(len(i["fields"]) for i in indices),
            "settingsMatched": len(settings_by_index),
        }

    # settings-only
    settings_by_index = {}
    for u in setting_files:
        settings_by_index.update(_parse_settings(u["text"], u["name"]))
    if not settings_by_index:
        raise BuildError("No Elasticsearch indices could be extracted from the uploaded file(s)")
    records = [
        {
            "__type": "es_settings",
            "path": s["sourcePath"],
            "indexName": index_name,
            "shards": s.get("shards"),
            "replicas": s.get("replicas"),
            "defaultAnalyzer": s.get("defaultAnalyzer"),
        }
        for index_name, s in settings_by_index.items()
    ]
    return {
        "records": records,
        "kind": "settings-only",
        "mapping": None,
        "setting": {"name": setting_files[0]["name"]},
        "mappings": [],
        "settings": [{"name": u["name"]} for u in setting_files],
        "indexCount": 0,
        "fieldCount": 0,
        "settingsMatched": len(settings_by_index),
    }
