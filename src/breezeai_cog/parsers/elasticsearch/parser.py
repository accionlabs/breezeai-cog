"""ElasticsearchParser — extracts index mappings from Elasticsearch JSON mapping files.

Handles ``.json`` files that contain an Elasticsearch mapping definition::

    {
      "mappings": {
        "properties": {
          "email": { "type": "keyword" },
          "name":  { "type": "text", "analyzer": "standard" }
        }
      },
      "settings": { "number_of_shards": 1 }
    }

Priority 1 overrides the config/structured-json parsers. The ``claims()`` guard
restricts activation to files that have both ``"mappings"`` and ``"properties"``
keys, plus at least one Elasticsearch field type value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...emit import class_id, disambiguate, file_id
from ...schemas import SCHEMA_VERSION, Class, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext

_ES_FIELD_TYPES = frozenset(
    {"keyword", "text", "integer", "long", "float", "double", "boolean",
     "date", "object", "nested", "geo_point", "ip", "binary", "completion"}
)

_ES_MARKERS = (b'"mappings"', b"'mappings'", b"mappings:")


class ElasticsearchParser(BaseParser):
    """Parses Elasticsearch JSON index mapping files."""

    name = "elasticsearch"
    extensions: tuple[str, ...] = (".json",)
    priority = 1
    schema_version = SCHEMA_VERSION
    statement_types: list[str] = []
    frameworks: list[str] = []

    def claims(self, path: str, source: bytes) -> bool:
        if not any(m in source for m in _ES_MARKERS):
            return False
        if b'"properties"' not in source and b"'properties'" not in source:
            return False
        # At least one ES field type value must appear
        return any(
            (f'"type": "{t}"').encode() in source or (f"'type': '{t}'").encode() in source
            for t in _ES_FIELD_TYPES
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        source = ctx.source
        path = ctx.path
        text = source.decode("utf-8", "replace")
        fid = file_id(path)
        seen_ids: set[str] = set()
        classes: list[Class] = []

        try:
            data: Any = json.loads(text)
        except Exception:
            data = None

        if isinstance(data, dict):
            # Single index mapping file
            cls = _make_index_class(data, Path(path).stem, path, fid, seen_ids)
            if cls is not None:
                classes.append(cls)

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="elasticsearch",
            loc=count_loc(text),
            classes=classes,
        )


def _make_index_class(
    data: dict[str, Any],
    index_name: str,
    path: str,
    fid: str,
    seen_ids: set[str],
) -> Class | None:
    mappings = data.get("mappings")
    if not isinstance(mappings, dict):
        return None
    properties = mappings.get("properties")
    if not isinstance(properties, dict):
        return None

    columns = _extract_properties(properties)
    if not columns:
        return None

    settings_raw = data.get("settings") or {}
    settings: dict = {}
    for key in ("number_of_shards", "numberOfShards", "number_of_replicas", "numberOfReplicas"):
        if key in settings_raw:
            try:
                settings[key] = int(settings_raw[key])
            except (ValueError, TypeError):
                pass

    cid = disambiguate(class_id(path, index_name), seen_ids)
    cls_kwargs: dict = dict(
        id=cid,
        parentId=fid,
        path=path,
        name=index_name,
        type="index_mapping",
        startLine=1,
        endLine=1,
        source="elasticsearch",
        columns=columns,
    )
    if settings:
        cls_kwargs["settings"] = settings
    return Class(**cls_kwargs)


def _extract_properties(properties: dict[str, Any]) -> list[dict]:
    """Recursively extract field definitions from an ES properties object."""
    columns: list[dict] = []
    for field_name, field_def in properties.items():
        if not isinstance(field_def, dict):
            continue
        data_type = field_def.get("type", "object")
        col: dict = {"name": field_name, "dataType": data_type}
        if "analyzer" in field_def:
            col["analyzer"] = field_def["analyzer"]
        # Nested properties
        nested_props = field_def.get("properties")
        if isinstance(nested_props, dict):
            nested = _extract_properties(nested_props)
            if nested:
                col["nested"] = nested
        columns.append(col)
    return columns
