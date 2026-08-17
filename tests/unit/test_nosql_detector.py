"""Tests for the TypeScript NoSQL schema additive detector."""

from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.typescript.parser import TypeScriptParser


def _parse(tmp_path, name: str, src: bytes, *, capture: bool = False):
    p = tmp_path / name
    p.write_bytes(src)
    ctx = ParseContext(
        path=name,
        abs_path=p,
        source=src,
        repo_root=tmp_path,
        capture_statements=capture,
    )
    return TypeScriptParser().parse_file(ctx)


# ---------------------------------------------------------------------------
# Pattern A: Mongoose new Schema({...})
# ---------------------------------------------------------------------------

MONGOOSE_SIMPLE = b"""
import mongoose, { Schema } from 'mongoose';

const UserSchema = new Schema({
  email: { type: String, required: true, unique: true },
  age: { type: Number },
  active: Boolean,
});
"""


def test_mongoose_simple_fields(tmp_path):
    rec = _parse(tmp_path, "user.schema.ts", MONGOOSE_SIMPLE)
    nosql = [c for c in rec.classes if c.type == "collection"]
    assert len(nosql) == 1
    cls = nosql[0]
    assert cls.name == "UserSchema"
    assert cls.type == "collection"
    # extra fields
    assert getattr(cls, "source", None) == "mongoose"
    columns = getattr(cls, "columns", [])
    names = [col["name"] for col in columns]
    assert "email" in names
    assert "age" in names
    assert "active" in names
    email_col = next(c for c in columns if c["name"] == "email")
    assert email_col["dataType"] == "String"
    assert email_col.get("required") is True
    assert email_col.get("unique") is True


MONGOOSE_NESTED = b"""
import { Schema } from 'mongoose';

const ProfileSchema = new Schema({
  firstName: String,
  lastName: String,
});

const UserSchema = new Schema({
  email: { type: String, required: true },
  profile: new Schema({ firstName: String, lastName: String }),
});
"""


def test_mongoose_nested_embedded(tmp_path):
    rec = _parse(tmp_path, "user.schema.ts", MONGOOSE_NESTED)
    nosql = [c for c in rec.classes if c.type == "collection"]
    assert any(c.name == "UserSchema" for c in nosql)
    user_cls = next(c for c in nosql if c.name == "UserSchema")
    columns = getattr(user_cls, "columns", [])
    profile_col = next((c for c in columns if c["name"] == "profile"), None)
    assert profile_col is not None
    assert profile_col["dataType"] == "Object"


# ---------------------------------------------------------------------------
# Pattern B: NestJS @Schema() + @Prop()
# ---------------------------------------------------------------------------

NESTJS_SCHEMA = b"""
import { Schema, Prop, SchemaFactory } from '@nestjs/mongoose';

@Schema({ collection: 'users' })
export class User {
  @Prop({ required: true })
  email: string;

  @Prop()
  name: string;

  regularField: number;
}
"""


def test_nestjs_schema_class(tmp_path):
    rec = _parse(tmp_path, "user.schema.ts", NESTJS_SCHEMA)
    nosql = [c for c in rec.classes if c.type == "collection"]
    assert len(nosql) >= 1
    schema_cls = next(
        (c for c in nosql if "User" in c.name and getattr(c, "source", None) == "nestjs-mongoose"),
        None,
    )
    assert schema_cls is not None, (
        f"Expected nestjs-mongoose collection, got: {[c.name for c in nosql]}"
    )
    columns = getattr(schema_cls, "columns", [])
    col_names = [col["name"] for col in columns]
    assert "email" in col_names
    assert "name" in col_names
    # regularField (no @Prop) should NOT be extracted
    assert "regularField" not in col_names
    # collection name
    assert getattr(schema_cls, "collectionName", None) == "users"


# ---------------------------------------------------------------------------
# Pattern C: DynamoDB CDK new Table(...)
# ---------------------------------------------------------------------------

DYNAMODB_TABLE = b"""
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Stack } from 'aws-cdk-lib';

const usersTable = new dynamodb.Table(this, 'UsersTable', {
  partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
  tableName: 'users',
});
"""


def test_dynamodb_table(tmp_path):
    rec = _parse(tmp_path, "stack.ts", DYNAMODB_TABLE)
    nosql = [c for c in rec.classes if c.type == "table"]
    assert len(nosql) >= 1
    tbl = nosql[0]
    assert getattr(tbl, "source", None) == "dynamodb"
    columns = getattr(tbl, "columns", [])
    # Should have PK and SK columns
    col_names = [col["name"] for col in columns]
    assert "PK" in col_names
    assert "SK" in col_names


# ---------------------------------------------------------------------------
# Negative tests
# ---------------------------------------------------------------------------

NO_NOSQL = b"""
export class FooService {
  doSomething(): void {
    console.log('hello');
  }
}
"""


def test_no_nosql_patterns_returns_empty(tmp_path):
    rec = _parse(tmp_path, "foo.service.ts", NO_NOSQL)
    nosql = [
        c
        for c in rec.classes
        if c.type in ("collection", "table", "graph_node", "graph_relationship")
    ]
    assert len(nosql) == 0


ONLY_MONGOOSE_NO_DYNAMODB = b"""
import { Schema } from 'mongoose';

const OrderSchema = new mongoose.Schema({
  orderId: String,
});
"""


def test_mongoose_only_no_dynamodb(tmp_path):
    rec = _parse(tmp_path, "order.schema.ts", ONLY_MONGOOSE_NO_DYNAMODB)
    tables = [c for c in rec.classes if c.type == "table"]
    # Should not produce any DynamoDB tables
    assert len(tables) == 0


# ---------------------------------------------------------------------------
# Pattern D: Neo4j @Node
# ---------------------------------------------------------------------------

NEO4J_NODE = b"""
import { Node, Property } from '@neo4j/nestjs-ogm';

@Node({ label: 'Persona' })
export class PersonaGraph {
  name: string;
  projectUuid: string;
}
"""


def test_neo4j_node_class(tmp_path):
    rec = _parse(tmp_path, "persona.graph.ts", NEO4J_NODE)
    graph_nodes = [c for c in rec.classes if c.type == "graph_node"]
    assert len(graph_nodes) >= 1
    node = graph_nodes[0]
    assert node.name == "PersonaGraph"
    assert getattr(node, "source", None) == "neo4j"
    assert getattr(node, "label", None) == "Persona"
    columns = getattr(node, "columns", [])
    col_names = [col["name"] for col in columns]
    assert "name" in col_names
    assert "projectUuid" in col_names
    name_col = next(c for c in columns if c["name"] == "name")
    assert name_col["dataType"] == "string"


# ---------------------------------------------------------------------------
# Mongoose ref: field — cross-schema linking
# ---------------------------------------------------------------------------

MONGOOSE_WITH_REF = b"""
import mongoose, { Schema } from 'mongoose';

const TaskSchema = new Schema({
  title: { type: String, required: true },
  project: { type: Schema.Types.ObjectId, ref: 'Project' },
  owner: { type: Schema.Types.ObjectId, ref: 'User' },
});
"""


def test_mongoose_ref_extraction(tmp_path):
    rec = _parse(tmp_path, "task.schema.ts", MONGOOSE_WITH_REF)
    nosql = [c for c in rec.classes if c.type == "collection"]
    assert len(nosql) >= 1
    cls = nosql[0]
    columns = getattr(cls, "columns", [])
    project_col = next((c for c in columns if c["name"] == "project"), None)
    assert project_col is not None
    assert project_col.get("ref") == "Project"
    owner_col = next((c for c in columns if c["name"] == "owner"), None)
    assert owner_col is not None
    assert owner_col.get("ref") == "User"


def test_mongoose_no_ref_on_plain_fields(tmp_path):
    rec = _parse(tmp_path, "user.schema.ts", MONGOOSE_SIMPLE)
    nosql = [c for c in rec.classes if c.type == "collection"]
    cls = nosql[0]
    columns = getattr(cls, "columns", [])
    email_col = next(c for c in columns if c["name"] == "email")
    assert "ref" not in email_col


# ---------------------------------------------------------------------------
# DynamoDB CDK GSI / LSI detection
# ---------------------------------------------------------------------------

DYNAMODB_WITH_GSI = b"""
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';

const ordersTable = new dynamodb.Table(this, 'OrdersTable', {
  partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
  tableName: 'orders',
});

ordersTable.addGlobalSecondaryIndex({
  indexName: 'GSI1',
  partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
  sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
});

ordersTable.addGlobalSecondaryIndex({
  indexName: 'GSI2',
  partitionKey: { name: 'customerId', type: dynamodb.AttributeType.STRING },
});
"""


def test_dynamodb_gsi_indexes_detected(tmp_path):
    rec = _parse(tmp_path, "stack.ts", DYNAMODB_WITH_GSI)
    tables = [c for c in rec.classes if c.type == "table"]
    assert len(tables) >= 1
    tbl = tables[0]
    indexes = getattr(tbl, "indexes", [])
    assert len(indexes) == 2
    index_names = {idx.get("name") for idx in indexes}
    assert "GSI1" in index_names
    assert "GSI2" in index_names


def test_dynamodb_gsi_type_label(tmp_path):
    rec = _parse(tmp_path, "stack.ts", DYNAMODB_WITH_GSI)
    tables = [c for c in rec.classes if c.type == "table"]
    tbl = tables[0]
    indexes = getattr(tbl, "indexes", [])
    for idx in indexes:
        assert idx.get("type") == "global_secondary_index"


def test_dynamodb_gsi_partition_key(tmp_path):
    rec = _parse(tmp_path, "stack.ts", DYNAMODB_WITH_GSI)
    tables = [c for c in rec.classes if c.type == "table"]
    tbl = tables[0]
    indexes = getattr(tbl, "indexes", [])
    gsi1 = next(i for i in indexes if i.get("name") == "GSI1")
    assert gsi1.get("partitionKey") == "GSI1PK"
    assert gsi1.get("sortKey") == "GSI1SK"


# ---------------------------------------------------------------------------
# Aggregation pipeline detection
# ---------------------------------------------------------------------------

MONGOOSE_AGGREGATION = b"""
import mongoose from 'mongoose';
const User = mongoose.model('User');

const usersByRole = await User.aggregate([
  { $match: { active: true } },
  { $group: { _id: '$role', count: { $sum: 1 } } },
]);
"""


def test_mongoose_aggregation_pipeline(tmp_path):
    rec = _parse(tmp_path, "user.repo.ts", MONGOOSE_AGGREGATION)
    pipelines = [f for f in rec.functions if f.type == "aggregation_pipeline"]
    assert len(pipelines) >= 1
    fn = pipelines[0]
    assert fn.name == "usersByRole"
    assert fn.type == "aggregation_pipeline"


# ---------------------------------------------------------------------------
# Schema validator extraction
# ---------------------------------------------------------------------------

MONGOOSE_WITH_VALIDATOR = b"""
import { Schema } from 'mongoose';

const EmailSchema = new Schema(
  { email: { type: String, required: true } },
  {
    validator: { email: { '$regex': '^.+@.+$' } },
    validationLevel: 'strict',
  }
);
"""


def test_mongoose_schema_validator(tmp_path):
    rec = _parse(tmp_path, "email.schema.ts", MONGOOSE_WITH_VALIDATOR)
    nosql = [c for c in rec.classes if c.type == "collection"]
    assert len(nosql) >= 1
    cls = nosql[0]
    metadata = getattr(cls, "metadata", None)
    assert metadata is not None
    validation = metadata.get("validation")
    assert validation is not None
    assert "validator" in validation
    assert validation.get("validationLevel") == "strict"


# ---------------------------------------------------------------------------
# Redis key schema detection
# ---------------------------------------------------------------------------

REDIS_KEY_SCHEMA = b"""
export const CACHE_KEY_SCHEMAS = [
  { pattern: 'session:{userId}', dataType: 'hash', ttl: 3600 },
  { pattern: 'cache:user:{userId}', dataType: 'string', ttl: 300 },
  { pattern: 'queue:emails', dataType: 'list' },
];
"""


def test_redis_key_schema(tmp_path):
    rec = _parse(tmp_path, "redis.keys.ts", REDIS_KEY_SCHEMA)
    schemas = [c for c in rec.classes if c.type == "key_schema"]
    assert len(schemas) >= 1
    ks = schemas[0]
    assert getattr(ks, "source", None) == "redis"
    patterns = getattr(ks, "patterns", [])
    assert len(patterns) >= 2
    pat_strings = [p["pattern"] for p in patterns]
    assert "session:{userId}" in pat_strings
    assert "cache:user:{userId}" in pat_strings


# ---------------------------------------------------------------------------
# Elasticsearch TS mapping detection
# ---------------------------------------------------------------------------

ES_MAPPING_TS = b"""
const usersMapping = {
  index: 'users_index',
  mappings: {
    properties: {
      email: { type: 'keyword' },
      name: { type: 'text', analyzer: 'standard' },
      createdAt: { type: 'date' },
    },
  },
  settings: {
    numberOfShards: 1,
    numberOfReplicas: 1,
  },
};
"""


def test_elasticsearch_ts_mapping(tmp_path):
    rec = _parse(tmp_path, "users.mapping.ts", ES_MAPPING_TS)
    mappings = [c for c in rec.classes if c.type == "index_mapping"]
    assert len(mappings) >= 1
    idx = mappings[0]
    assert getattr(idx, "source", None) == "elasticsearch"
    columns = getattr(idx, "columns", [])
    col_names = [col["name"] for col in columns]
    assert "email" in col_names
    assert "name" in col_names
    email_col = next(c for c in columns if c["name"] == "email")
    assert email_col["dataType"] == "keyword"


# ---------------------------------------------------------------------------
# Fix 1 — MongoDB field attributes: enum, default, index, itemType
# ---------------------------------------------------------------------------

MONGOOSE_FIELD_ATTRS = b"""
import { Schema } from 'mongoose';
const UserSchema = new Schema({
  role: { type: String, enum: ['admin', 'viewer', 'editor'], default: 'viewer' },
  email: { type: String, index: true },
});
"""


def test_mongoose_enum_and_default(tmp_path):
    rec = _parse(tmp_path, "user.schema.ts", MONGOOSE_FIELD_ATTRS)
    nosql = [c for c in rec.classes if c.type == "collection"]
    cls = nosql[0]
    columns = getattr(cls, "columns", [])
    role_col = next(c for c in columns if c["name"] == "role")
    assert role_col.get("enum") == ["admin", "viewer", "editor"]
    assert role_col.get("default") == "viewer"


def test_mongoose_index_flag(tmp_path):
    rec = _parse(tmp_path, "user.schema.ts", MONGOOSE_FIELD_ATTRS)
    nosql = [c for c in rec.classes if c.type == "collection"]
    cls = nosql[0]
    columns = getattr(cls, "columns", [])
    email_col = next(c for c in columns if c["name"] == "email")
    assert email_col.get("index") is True


# ---------------------------------------------------------------------------
# Fix 1 — itemType for array fields
# ---------------------------------------------------------------------------

MONGOOSE_ARRAY_FIELD = b"""
import { Schema } from 'mongoose';
const PostSchema = new Schema({
  tags: [String],
  scores: [Number],
});
"""


def test_mongoose_array_item_type(tmp_path):
    rec = _parse(tmp_path, "post.schema.ts", MONGOOSE_ARRAY_FIELD)
    nosql = [c for c in rec.classes if c.type == "collection"]
    cls = nosql[0]
    columns = getattr(cls, "columns", [])
    tags_col = next(c for c in columns if c["name"] == "tags")
    assert tags_col["dataType"] == "Array"
    assert tags_col.get("itemType") == "String"


# ---------------------------------------------------------------------------
# Fix 4 — Mongoose schema-level indexes[]
# ---------------------------------------------------------------------------

MONGOOSE_SCHEMA_INDEX = b"""
import { Schema } from 'mongoose';
const UserSchema = new Schema({
  email: { type: String },
  role: { type: String },
  createdAt: { type: Date },
});
UserSchema.index({ email: 1 }, { unique: true });
UserSchema.index({ role: 1, createdAt: -1 });
"""


def test_mongoose_schema_indexes(tmp_path):
    rec = _parse(tmp_path, "user.schema.ts", MONGOOSE_SCHEMA_INDEX)
    nosql = [c for c in rec.classes if c.type == "collection"]
    cls = nosql[0]
    indexes = getattr(cls, "indexes", [])
    assert len(indexes) >= 1
    email_idx = next((i for i in indexes if "email" in i.get("columns", [])), None)
    assert email_idx is not None
    assert email_idx.get("unique") is True


# ---------------------------------------------------------------------------
# Fix 5 — collectionName from mongoose.model()
# ---------------------------------------------------------------------------

MONGOOSE_MODEL_CALL = b"""
import mongoose, { Schema } from 'mongoose';
const UserSchema = new Schema({ email: String });
const User = mongoose.model('User', UserSchema);
"""


def test_mongoose_collection_name_from_model_call(tmp_path):
    rec = _parse(tmp_path, "user.model.ts", MONGOOSE_MODEL_CALL)
    nosql = [c for c in rec.classes if c.type == "collection"]
    cls = nosql[0]
    assert getattr(cls, "collectionName", None) == "User"


# ---------------------------------------------------------------------------
# Fix 3 — DynamoDB billingMode
# ---------------------------------------------------------------------------

DYNAMODB_WITH_BILLING = b"""
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
const table = new dynamodb.Table(this, 'T', {
  partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
  billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
});
"""


def test_dynamodb_billing_mode(tmp_path):
    rec = _parse(tmp_path, "stack.ts", DYNAMODB_WITH_BILLING)
    tables = [c for c in rec.classes if c.type == "table"]
    tbl = tables[0]
    assert getattr(tbl, "billingMode", None) == "PAY_PER_REQUEST"


# ---------------------------------------------------------------------------
# Fix 2 — DynamoDB index dict key names (type / name)
# ---------------------------------------------------------------------------


def test_dynamodb_gsi_type_label_new_keys(tmp_path):
    rec = _parse(tmp_path, "stack.ts", DYNAMODB_WITH_GSI)
    tables = [c for c in rec.classes if c.type == "table"]
    tbl = tables[0]
    indexes = getattr(tbl, "indexes", [])
    for idx in indexes:
        assert idx.get("type") == "global_secondary_index"


def test_dynamodb_gsi_partition_key_new_keys(tmp_path):
    rec = _parse(tmp_path, "stack.ts", DYNAMODB_WITH_GSI)
    tables = [c for c in rec.classes if c.type == "table"]
    tbl = tables[0]
    indexes = getattr(tbl, "indexes", [])
    gsi1 = next(i for i in indexes if i.get("name") == "GSI1")
    assert gsi1.get("partitionKey") == "GSI1PK"
    assert gsi1.get("sortKey") == "GSI1SK"


# ---------------------------------------------------------------------------
# Fix 6 — Neo4j relationships[]
# ---------------------------------------------------------------------------

NEO4J_WITH_RELATIONSHIPS = b"""
import { Node, RelatedTo } from '@neo4j/nestjs-ogm';

@Node({ label: 'Persona' })
export class PersonaGraph {
  name: string;

  @RelatedTo({ target: 'OutcomeGraph', relationship: 'HAS_OUTCOME', direction: 'out' })
  outcomes: any[];
}
"""


def test_neo4j_relationships_extracted(tmp_path):
    rec = _parse(tmp_path, "persona.graph.ts", NEO4J_WITH_RELATIONSHIPS)
    graph_nodes = [c for c in rec.classes if c.type == "graph_node"]
    node = graph_nodes[0]
    relationships = getattr(node, "relationships", [])
    assert len(relationships) >= 1
    rel = relationships[0]
    assert rel.get("type") == "HAS_OUTCOME"
    assert rel.get("target") == "OutcomeGraph"
    assert rel.get("direction") == "outgoing"


# ---------------------------------------------------------------------------
# Gap 3 — NestJS @Prop() attribute parity
# ---------------------------------------------------------------------------

NESTJS_PROP_ATTRS = b"""
import { Schema, Prop, SchemaFactory } from '@nestjs/mongoose';

@Schema()
export class Product {
  @Prop({ required: true, enum: ['active', 'inactive'], default: 'active', index: true })
  status: string;

  @Prop({ unique: true })
  sku: string;
}
"""


def test_nestjs_prop_enum_default(tmp_path):
    rec = _parse(tmp_path, "product.schema.ts", NESTJS_PROP_ATTRS)
    nosql = [c for c in rec.classes if c.type == "collection"]
    assert len(nosql) >= 1
    cls = nosql[0]
    columns = getattr(cls, "columns", [])
    status_col = next((c for c in columns if c["name"] == "status"), None)
    assert status_col is not None
    assert status_col.get("enum") == ["active", "inactive"]
    assert status_col.get("default") == "active"
    assert status_col.get("index") is True
    assert status_col.get("required") is True


def test_nestjs_prop_unique(tmp_path):
    rec = _parse(tmp_path, "product.schema.ts", NESTJS_PROP_ATTRS)
    nosql = [c for c in rec.classes if c.type == "collection"]
    cls = nosql[0]
    columns = getattr(cls, "columns", [])
    sku_col = next((c for c in columns if c["name"] == "sku"), None)
    assert sku_col is not None
    assert sku_col.get("unique") is True


# ---------------------------------------------------------------------------
# Gap 4 — Neo4j constraints[] extraction
# ---------------------------------------------------------------------------

NEO4J_WITH_CONSTRAINTS = b"""
import { Node } from '@neo4j/nestjs-ogm';

@Node({ label: 'Person', constraints: [{ type: 'NODE_KEY', columns: ['id'] }] })
export class PersonNode {
  id: string;
  name: string;
}
"""


def test_neo4j_constraints_extracted(tmp_path):
    rec = _parse(tmp_path, "person.graph.ts", NEO4J_WITH_CONSTRAINTS)
    graph_nodes = [c for c in rec.classes if c.type == "graph_node"]
    assert len(graph_nodes) >= 1
    node = graph_nodes[0]
    constraints = getattr(node, "constraints", [])
    assert len(constraints) >= 1
    c = constraints[0]
    assert c.get("type") == "NODE_KEY"
    assert c.get("columns") == ["id"]


# ---------------------------------------------------------------------------
# Gap 5 — NestJS bare @Prop() (no arguments) should not crash and skips attrs
# ---------------------------------------------------------------------------

NESTJS_BARE_PROP = b"""
import { Schema, Prop } from '@nestjs/mongoose';

@Schema()
export class Item {
  @Prop()
  name: string;

  @Prop({ required: true })
  code: string;
}
"""


def test_nestjs_bare_prop_no_crash(tmp_path):
    rec = _parse(tmp_path, "item.schema.ts", NESTJS_BARE_PROP)
    nosql = [c for c in rec.classes if c.type == "collection"]
    assert len(nosql) >= 1
    cls = nosql[0]
    columns = getattr(cls, "columns", [])
    col_names = [c["name"] for c in columns]
    assert "name" in col_names
    assert "code" in col_names
    name_col = next(c for c in columns if c["name"] == "name")
    assert "required" not in name_col  # bare @Prop() has no config
    code_col = next(c for c in columns if c["name"] == "code")
    assert code_col.get("required") is True
