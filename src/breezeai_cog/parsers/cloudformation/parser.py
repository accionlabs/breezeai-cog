"""CloudFormationParser — extracts DynamoDB table schemas from AWS CloudFormation templates.

Handles both YAML (``.yaml``/``.yml``) and JSON (``.json``) CloudFormation files.
Detected resources:
- ``AWS::DynamoDB::Table`` → Class with type="table", columns (key schema),
  and indexes[] (GSI/LSI definitions) for cross-schema analysis.

Priority 1 overrides the config parser (priority 0) for CloudFormation files.
The ``claims()`` guard keeps non-CF YAML/JSON files with their original parsers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from ...emit import class_id, disambiguate, file_id
from ...schemas import SCHEMA_VERSION, Class, FileRecord
from ...utils import count_loc
from ..base import BaseParser, ParseContext

_CF_MARKERS = (b"AWS::", b"AWSTemplateFormatVersion")
_DYNAMO_TYPE = "AWS::DynamoDB::Table"
_ATTR_TYPE_MAP = {"S": "String", "N": "Number", "B": "Binary"}


class CloudFormationParser(BaseParser):
    """Parses CloudFormation YAML/JSON templates for DynamoDB table schemas."""

    name = "cloudformation"
    extensions: tuple[str, ...] = (".yaml", ".yml", ".json")
    priority = 1  # beats config (0) and structured_json (0) for CF files
    schema_version = SCHEMA_VERSION
    statement_types: list[str] = []
    frameworks: list[str] = []

    def claims(self, path: str, source: bytes) -> bool:
        return (
            any(marker in source for marker in _CF_MARKERS)
            and _DYNAMO_TYPE.encode() in source
        )

    def parse_file(self, ctx: ParseContext) -> FileRecord:
        source = ctx.source
        path = ctx.path
        text = source.decode("utf-8", "replace")
        fid = file_id(path)
        seen_ids: set[str] = set()
        classes: list[Class] = []

        try:
            template: Any = (
                json.loads(text) if Path(path).suffix == ".json" else yaml.safe_load(text)
            )
        except Exception:
            template = None

        if isinstance(template, dict):
            for logical_name, resource in (template.get("Resources") or {}).items():
                if not isinstance(resource, dict):
                    continue
                if resource.get("Type") != _DYNAMO_TYPE:
                    continue
                props = resource.get("Properties") or {}
                cls = _make_table_class(logical_name, props, path, fid, seen_ids)
                classes.append(cls)

        return FileRecord(
            id=fid,
            path=path,
            type="code",
            language="cloudformation",
            loc=count_loc(text),
            classes=classes,
        )


def _make_table_class(
    logical_name: str,
    props: dict[str, Any],
    path: str,
    fid: str,
    seen_ids: set[str],
) -> Class:
    table_name = props.get("TableName") or logical_name

    # Build attribute-name → data-type map from AttributeDefinitions
    attr_types: dict[str, str] = {}
    for attr in props.get("AttributeDefinitions") or []:
        attr_name = attr.get("AttributeName", "")
        attr_types[attr_name] = _ATTR_TYPE_MAP.get(attr.get("AttributeType", "S"), "String")

    # Primary key columns from KeySchema
    columns: list[dict] = []
    for ks in props.get("KeySchema") or []:
        key_name = ks.get("AttributeName", "")
        columns.append({
            "name": key_name,
            "dataType": attr_types.get(key_name, "String"),
            "keyType": ks.get("KeyType", "HASH"),
        })

    # GSIs and LSIs
    indexes: list[dict] = []
    for gsi in props.get("GlobalSecondaryIndexes") or []:
        info = _parse_index(gsi, attr_types, "global_secondary_index")
        if info:
            indexes.append(info)
    for lsi in props.get("LocalSecondaryIndexes") or []:
        info = _parse_index(lsi, attr_types, "local_secondary_index")
        if info:
            indexes.append(info)

    billing_mode = props.get("BillingMode")

    cid = disambiguate(class_id(path, table_name), seen_ids)
    cls_kwargs: dict = dict(
        id=cid,
        parentId=fid,
        path=path,
        name=table_name,
        type="table",
        startLine=1,
        endLine=1,
        source="cloudformation",
        columns=columns,
        indexes=indexes,
    )
    if billing_mode:
        cls_kwargs["billingMode"] = billing_mode
    return Class(**cls_kwargs)


def _parse_index(
    index_def: dict[str, Any],
    attr_types: dict[str, str],
    index_type: str,
) -> dict | None:
    index_name = index_def.get("IndexName")
    if not index_name:
        return None
    info: dict = {"name": index_name, "type": index_type}
    for ks in index_def.get("KeySchema") or []:
        key_name = ks.get("AttributeName", "")
        key_type = ks.get("KeyType", "HASH")
        if key_type == "HASH":
            info["partitionKey"] = key_name
        elif key_type == "RANGE":
            info["sortKey"] = key_name
    projection = index_def.get("Projection") or {}
    info["projection"] = projection.get("ProjectionType", "ALL")
    return info
