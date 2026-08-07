"""Non-Relational (NoSQL) schema analyzer.

Normalizes an uploaded Non-Relational file into ``nosql_collection`` NDJSON
records that the backend's stream-ingest
(``DbOntologyGraphService.processNoSqlRecord``) consumes. The model is
engine-agnostic; engine-specific detail rides in the free-form ``attributes``
bag, serialized to a JSON string here so the graph can store it verbatim.

Three input shapes are accepted (per uploaded file):

1. An explicit **schema** document::

       {
         "collections": [
           {
             "name": "users",
             "source": "...",              # optional engine hint
             "description": "...",
             "attributes": { "shardKey": "userId" },   # optional, free-form
             "fields": [
               {"name": "email", "fullPath": "email", "dataType": "String",
                "nullable": false, "attributes": {"unique": true}},
               {"name": "firstName", "fullPath": "profile.firstName",
                "dataType": "String", "parentPath": "profile"}
             ],
             "indexes": [
               {"name": "email_idx", "fields": ["email"], "unique": true}
             ]
           }
         ]
       }

2. A **JSON array of documents** (a data dump) — the schema is inferred by
   sampling the documents; the collection name comes from the file name.

3. **NDJSON** — one JSON document per line — inferred the same way as (2).
"""

from __future__ import annotations

import json
from typing import Any


class BuildError(Exception):
    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _as_attr_string(value: Any) -> str | None:
    """Serialize an attributes bag to the JSON string the graph stores.

    Accepts an object (serialized), an already-encoded string (passed through),
    or any other JSON value (serialized). None / empty → None.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        return json.dumps(value) if value else None
    return json.dumps(value)


def _field(raw: Any, coll_name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BuildError(f"[nosql/{coll_name}] each field must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise BuildError(f"[nosql/{coll_name}] a field is missing a non-empty 'name'")
    data_type = raw.get("dataType")
    if not isinstance(data_type, str) or not data_type.strip():
        raise BuildError(f"[nosql/{coll_name}] field '{name}' is missing a non-empty 'dataType'")
    full_path = raw.get("fullPath")
    if not isinstance(full_path, str) or not full_path.strip():
        full_path = name
    parent_path = raw.get("parentPath")
    if not isinstance(parent_path, str) or not parent_path.strip():
        parent_path = None
    nullable = raw.get("nullable")
    description = raw.get("description")
    return {
        "name": name,
        "fullPath": full_path,
        "parentPath": parent_path,
        "dataType": data_type,
        "nullable": True if nullable is None else bool(nullable),
        "description": description if isinstance(description, str) else None,
        "attributes": _as_attr_string(raw.get("attributes")),
    }


def _index(raw: Any, coll_name: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BuildError(f"[nosql/{coll_name}] each index must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise BuildError(f"[nosql/{coll_name}] an index is missing a non-empty 'name'")
    fields = raw.get("fields")
    if not isinstance(fields, list) or not fields or not all(isinstance(f, str) for f in fields):
        raise BuildError(
            f"[nosql/{coll_name}] index '{name}' must have a non-empty 'fields' array of strings"
        )
    description = raw.get("description")
    return {
        "name": name,
        "fields": fields,
        "unique": bool(raw.get("unique", False)),
        "description": description if isinstance(description, str) else None,
        "attributes": _as_attr_string(raw.get("attributes")),
    }


def _collection(raw: Any, source_path: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BuildError("each entry in 'collections' must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise BuildError("a collection is missing a non-empty 'name'")
    fields_raw = raw.get("fields") or []
    indexes_raw = raw.get("indexes") or []
    if not isinstance(fields_raw, list):
        raise BuildError(f"[nosql/{name}] 'fields' must be an array")
    if not isinstance(indexes_raw, list):
        raise BuildError(f"[nosql/{name}] 'indexes' must be an array")
    description = raw.get("description")
    source = raw.get("source")
    return {
        "__type": "nosql_collection",
        "path": source_path,
        "name": name,
        "source": source if isinstance(source, str) else None,
        "description": description if isinstance(description, str) else None,
        "attributes": _as_attr_string(raw.get("attributes")),
        "fields": [_field(f, name) for f in fields_raw],
        "indexes": [_index(i, name) for i in indexes_raw],
    }


# ── Schema inference from data dumps ─────────────────────────────────────
# Real-world uploads are usually database EXPORTS (documents), not hand-authored
# schemas — a JSON array of documents or an NDJSON file (one document per line).
# We infer a collection schema by sampling those documents: the collection name
# comes from the file name; fields are the union of keys across sampled docs
# (dotted paths for nested objects), typed from the JSON value shapes.

_SAMPLE_LIMIT = 500
_MAX_DEPTH = 5

# Extended-JSON wrappers (a common convention for encoding typed values in a
# JSON document, e.g. {"$oid": "..."}) → a logical type. Detected if present so
# an id/date column isn't mislabelled "Object"; absent for plain-JSON dumps.
# Do not recurse into these.
_EXT_JSON_TYPE = {
    "$oid": "ObjectId",
    "$date": "Date",
    "$numberLong": "Long",
    "$numberInt": "Int",
    "$numberDouble": "Double",
    "$numberDecimal": "Decimal",
    "$binary": "Binary",
    "$timestamp": "Timestamp",
    "$regularExpression": "Regex",
    "$uuid": "UUID",
    "$ref": "DBRef",
}


def _collection_name_from_filename(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for suffix in (".nosql.json", ".ndjson", ".jsonl", ".json"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base or "collection"


def _infer_value_type(value: Any) -> tuple[str, dict | None]:
    """Return (dataType, nested_object_or_None) for one document value."""
    if value is None:
        return "Null", None
    if isinstance(value, bool):
        return "Boolean", None
    if isinstance(value, (int, float)):
        return "Number", None
    if isinstance(value, str):
        return "String", None
    if isinstance(value, list):
        return "Array", None
    if isinstance(value, dict):
        for key, typ in _EXT_JSON_TYPE.items():
            if key in value:
                return typ, None
        return "Object", value
    return "Unknown", None


def _parse_ndjson(text: str) -> list[Any]:
    """Parse one JSON value per non-empty line (NDJSON)."""
    docs: list[Any] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            docs.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return docs


def _infer_fields_from_documents(docs: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """Sample documents and infer a flat + nested field list (dotted paths)."""
    acc: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    sampled = 0

    def visit(obj: dict, prefix: str, parent: str | None, depth: int) -> None:
        for key, value in obj.items():
            full = f"{prefix}{key}"
            rec = acc.get(full)
            if rec is None:
                rec = {
                    "name": key,
                    "parentPath": parent,
                    "types": set(),
                    "present": 0,
                    "nulls": 0,
                }
                acc[full] = rec
                order.append(full)
            rec["present"] += 1
            dtype, nested = _infer_value_type(value)
            if dtype == "Null":
                rec["nulls"] += 1
            else:
                rec["types"].add(dtype)
            if nested is not None and depth < _MAX_DEPTH:
                visit(nested, full + ".", full, depth + 1)

    for doc in docs[:_SAMPLE_LIMIT]:
        if isinstance(doc, dict):
            sampled += 1
            visit(doc, "", None, 0)

    fields: list[dict[str, Any]] = []
    for full in order:
        rec = acc[full]
        types = rec["types"]
        if len(types) == 1:
            data_type = next(iter(types))
        elif not types:
            data_type = "Null"
        else:
            data_type = "Mixed"
        # Required only if present and non-null in every sampled document.
        nullable = rec["nulls"] > 0 or rec["present"] < sampled
        fields.append(
            {
                "name": rec["name"],
                "fullPath": full,
                "parentPath": rec["parentPath"],
                "dataType": data_type,
                "nullable": nullable,
                "attributes": None,
            }
        )
    return fields, sampled


def _infer_collection(docs: list[Any], collection_name: str, source_path: str) -> dict[str, Any]:
    fields, sampled = _infer_fields_from_documents(docs)
    if sampled == 0:
        raise BuildError(f"{source_path}: no JSON documents found to infer a schema from")
    return {
        "__type": "nosql_collection",
        "path": source_path,
        # Engine is unknown for an inferred data dump — left unset rather than
        # guessed. The `inferred` flag in attributes records provenance.
        "source": None,
        "name": collection_name,
        "description": f"Schema inferred from {sampled} sampled document(s).",
        "attributes": _as_attr_string({"inferred": True, "sampledDocuments": sampled}),
        "fields": fields,
        "indexes": [],
    }


def build_nosql_records(uploads: list[dict[str, Any]]) -> dict[str, Any]:
    """Parse uploaded Non-Relational files into ``nosql_collection`` records.

    Three input shapes are accepted per file (``{"name", "text", "size"}``):
      1. A schema document — ``{"collections": [{name, fields, indexes}, ...]}``.
      2. A JSON array of documents (data dump) — schema inferred by sampling.
      3. NDJSON — one JSON document per line — schema inferred by sampling.
    For (2) and (3) the collection name comes from the file name. Raises
    ``BuildError`` (400/422) on malformed input.
    """
    if not uploads:
        raise BuildError("At least one NoSQL schema or data file is required", 400)

    records: list[dict[str, Any]] = []
    for u in uploads:
        name = u["name"]
        text = u["text"]
        try:
            parsed: Any = json.loads(text)
            parse_ok = True
        except json.JSONDecodeError:
            parse_ok = False

        if parse_ok and isinstance(parsed, dict) and isinstance(parsed.get("collections"), list):
            # (1) explicit schema
            collections = parsed["collections"]
            if not collections:
                raise BuildError(f"{name} has an empty 'collections' array")
            for c in collections:
                records.append(_collection(c, name))
        elif parse_ok and isinstance(parsed, list):
            # (2) JSON array of documents → infer
            records.append(_infer_collection(parsed, _collection_name_from_filename(name), name))
        elif not parse_ok:
            # (3) NDJSON documents → infer
            docs = _parse_ndjson(text)
            if not docs:
                raise BuildError(
                    f"{name} is neither valid JSON (schema / document array) nor "
                    "NDJSON documents."
                )
            records.append(_infer_collection(docs, _collection_name_from_filename(name), name))
        else:
            # A bare JSON object that isn't a schema — treat as a single document.
            records.append(_infer_collection([parsed], _collection_name_from_filename(name), name))

    if not records:
        raise BuildError("No NoSQL collections could be extracted from the uploaded file(s)")

    return {
        "records": records,
        "collectionCount": len(records),
        "fieldCount": sum(len(r["fields"]) for r in records),
        "indexCount": sum(len(r["indexes"]) for r in records),
        "collections": [r["name"] for r in records],
    }
