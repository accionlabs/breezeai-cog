"""Tests for the CloudFormation parser (DynamoDB table schema extraction)."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.cloudformation.parser import CloudFormationParser


def _parse(tmp_path, name: str, src: bytes):
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(
        path=name,
        abs_path=p,
        source=src,
        repo_root=tmp_path,
    )
    return CloudFormationParser().parse_file(ctx)


CF_YAML_SIMPLE = b"""
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  UsersTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: users
      AttributeDefinitions:
        - AttributeName: PK
          AttributeType: S
        - AttributeName: SK
          AttributeType: S
      KeySchema:
        - AttributeName: PK
          KeyType: HASH
        - AttributeName: SK
          KeyType: RANGE
      BillingMode: PAY_PER_REQUEST
"""

CF_YAML_WITH_GSI = b"""
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  OrdersTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: orders
      AttributeDefinitions:
        - AttributeName: PK
          AttributeType: S
        - AttributeName: SK
          AttributeType: S
        - AttributeName: customerId
          AttributeType: S
        - AttributeName: createdAt
          AttributeType: S
      KeySchema:
        - AttributeName: PK
          KeyType: HASH
        - AttributeName: SK
          KeyType: RANGE
      GlobalSecondaryIndexes:
        - IndexName: GSI1-customerId
          KeySchema:
            - AttributeName: customerId
              KeyType: HASH
            - AttributeName: createdAt
              KeyType: RANGE
          Projection:
            ProjectionType: ALL
      LocalSecondaryIndexes:
        - IndexName: LSI1-createdAt
          KeySchema:
            - AttributeName: PK
              KeyType: HASH
            - AttributeName: createdAt
              KeyType: RANGE
          Projection:
            ProjectionType: ALL
"""

CF_JSON_SIMPLE = b"""{
  "AWSTemplateFormatVersion": "2010-09-09",
  "Resources": {
    "ProductsTable": {
      "Type": "AWS::DynamoDB::Table",
      "Properties": {
        "TableName": "products",
        "AttributeDefinitions": [
          {"AttributeName": "PK", "AttributeType": "S"}
        ],
        "KeySchema": [
          {"AttributeName": "PK", "KeyType": "HASH"}
        ]
      }
    }
  }
}"""

NON_CF_YAML = b"""
# Plain Kubernetes deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
"""


# ---------------------------------------------------------------------------
# claims() sniffing
# ---------------------------------------------------------------------------


def test_claims_cloudformation_yaml(tmp_path):
    p = tmp_path / "template.yaml"
    p.write_bytes(CF_YAML_SIMPLE)
    parser = CloudFormationParser()
    assert parser.claims("template.yaml", CF_YAML_SIMPLE) is True


def test_does_not_claim_plain_yaml(tmp_path):
    parser = CloudFormationParser()
    assert parser.claims("deploy.yaml", NON_CF_YAML) is False


def test_claims_cloudformation_json(tmp_path):
    parser = CloudFormationParser()
    assert parser.claims("template.json", CF_JSON_SIMPLE) is True


# ---------------------------------------------------------------------------
# Basic YAML table extraction
# ---------------------------------------------------------------------------


def test_yaml_table_detected(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_SIMPLE)
    tables = [c for c in rec.classes if c.type == "table"]
    assert len(tables) == 1


def test_yaml_table_name(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_SIMPLE)
    tables = [c for c in rec.classes if c.type == "table"]
    assert tables[0].name == "users"


def test_yaml_table_source(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_SIMPLE)
    tables = [c for c in rec.classes if c.type == "table"]
    assert getattr(tables[0], "source", None) == "cloudformation"


def test_yaml_table_primary_key_columns(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_SIMPLE)
    tables = [c for c in rec.classes if c.type == "table"]
    columns = getattr(tables[0], "columns", [])
    col_names = [c["name"] for c in columns]
    assert "PK" in col_names
    assert "SK" in col_names
    pk = next(c for c in columns if c["name"] == "PK")
    assert pk["keyType"] == "HASH"
    sk = next(c for c in columns if c["name"] == "SK")
    assert sk["keyType"] == "RANGE"


def test_yaml_table_language(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_SIMPLE)
    assert rec.language == "cloudformation"


# ---------------------------------------------------------------------------
# GSI / LSI extraction
# ---------------------------------------------------------------------------


def test_yaml_gsi_detected(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_WITH_GSI)
    tables = [c for c in rec.classes if c.type == "table"]
    assert len(tables) == 1
    indexes = getattr(tables[0], "indexes", [])
    gsis = [i for i in indexes if i.get("type") == "global_secondary_index"]
    assert len(gsis) == 1
    assert gsis[0]["name"] == "GSI1-customerId"
    assert gsis[0]["partitionKey"] == "customerId"
    assert gsis[0]["sortKey"] == "createdAt"


def test_yaml_lsi_detected(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_WITH_GSI)
    tables = [c for c in rec.classes if c.type == "table"]
    indexes = getattr(tables[0], "indexes", [])
    lsis = [i for i in indexes if i.get("type") == "local_secondary_index"]
    assert len(lsis) == 1
    assert lsis[0]["name"] == "LSI1-createdAt"


def test_yaml_no_indexes_for_simple_table(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_SIMPLE)
    tables = [c for c in rec.classes if c.type == "table"]
    indexes = getattr(tables[0], "indexes", [])
    assert indexes == []


def test_yaml_billing_mode_extracted(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_SIMPLE)
    tables = [c for c in rec.classes if c.type == "table"]
    assert getattr(tables[0], "billingMode", None) == "PAY_PER_REQUEST"


# ---------------------------------------------------------------------------
# JSON template
# ---------------------------------------------------------------------------


def test_json_table_detected(tmp_path):
    rec = _parse(tmp_path, "template.json", CF_JSON_SIMPLE)
    tables = [c for c in rec.classes if c.type == "table"]
    assert len(tables) == 1
    assert tables[0].name == "products"


def test_json_table_hash_key(tmp_path):
    rec = _parse(tmp_path, "template.json", CF_JSON_SIMPLE)
    tables = [c for c in rec.classes if c.type == "table"]
    columns = getattr(tables[0], "columns", [])
    assert any(c["name"] == "PK" and c["keyType"] == "HASH" for c in columns)


# ---------------------------------------------------------------------------
# Multi-resource template
# ---------------------------------------------------------------------------

CF_YAML_MULTI = b"""
AWSTemplateFormatVersion: '2010-09-09'
Resources:
  UsersTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: users
      AttributeDefinitions:
        - AttributeName: PK
          AttributeType: S
      KeySchema:
        - AttributeName: PK
          KeyType: HASH
  SessionsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      TableName: sessions
      AttributeDefinitions:
        - AttributeName: token
          AttributeType: S
      KeySchema:
        - AttributeName: token
          KeyType: HASH
  MyBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: my-bucket
"""


def test_multi_resource_only_dynamo_extracted(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_MULTI)
    tables = [c for c in rec.classes if c.type == "table"]
    assert len(tables) == 2
    table_names = {t.name for t in tables}
    assert "users" in table_names
    assert "sessions" in table_names


def test_non_dynamo_resources_not_emitted(tmp_path):
    rec = _parse(tmp_path, "template.yaml", CF_YAML_MULTI)
    assert len(rec.classes) == 2  # no S3 class
