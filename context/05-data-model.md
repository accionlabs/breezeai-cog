---
title: "breezeai-cog — Data Model (Capture Contract)"
date: 2026-10-30
---

Scope: The NDJSON capture contract — record types, fields, enums, schema version, JSON Schema generation. Source of truth: `src/breezeai_cog/schemas/`.

## Schema version

`SCHEMA_VERSION = "2.4"` — defined in `src/breezeai_cog/schemas/version.py:20`.

History of minor additions (in comments in `version.py`):
- 2.1: additive `Statement.isPartial`
- 2.2: `Statement.platform`, `Statement.endpoint` (IaC), `iac_data` semantic type, module blocks → `Class`
- 2.3: additive `Statement.handlerPath`
- 2.4: additive optional `Class.alias` (the tag of a C/C++ `typedef struct _tag { ... } name;` when it differs from `name`)

The Pydantic models in `schemas/capture.py` are the **source of truth**. The JSON Schema is *generated on demand* (`breezeai-cog schema` CLI, or `export_json_schema()`) for cross-language consumers (Node backend). It is not committed — generate it when a consumer needs it.

## NDJSON structure

Every `.ndjson.gz` file from cog:
- **Line 1**: `ProjectMetaData` record (`__type: "projectMetaData"`)
- **Line 2+**: `FileRecord` records (one per source file; `type: "code"` or `"config"`)

## Record types

### ProjectMetaData

Source: `src/breezeai_cog/schemas/capture.py:208`

| Field | Type | Notes |
|---|---|---|
| `__type` | `"projectMetaData"` | Discriminator (aliased from `record_type`) |
| `repositoryName` | `str` | Required |
| `analyzedLanguages` | `list[str]` | Required |
| `totalFiles` | `int` | Required |
| `totalFunctions` | `int` | Required |
| `totalClasses` | `int` | Required |
| `totalLinesOfCode` | `int` | Required |
| `generatedAt` | `str` | ISO-8601 datetime |
| `toolVersion` | `str` | Required |
| `repositoryPath` | `str \| None` | Optional |
| `configs` | `dict \| None` | Config-file summary: `{totalConfigFiles, capturedDataDocuments, byType, packageManagers, buildTools, docker, dependencies}` |

### FileRecord

Source: `src/breezeai_cog/schemas/capture.py:176`
Open node (`extra="allow"` — custom attributes preserved).

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | Required |
| `path` | `str` | Required; repo-relative |
| `type` | `FileType` | `"code"` or `"config"` |
| `language` | `str` | Required |
| `loc` | `int` | Lines of code |
| `framework` | `str \| None` | Optional |
| `platform` | `str \| None` | IaC-only: dominant cloud provider |
| `uiRole` | `str \| None` | `component`/`page`/`layout`/`store`/`composable`/`hook`/`directive` (open string) |
| `importFiles` | `list[str]` | In-repo paths → IMPORTS edges |
| `externalImports` | `list[str]` | External package imports |
| `exports` | `list[str]` | |
| `functions` | `list[Function]` | Code files only |
| `classes` | `list[Class]` | Code files only |
| `statements` | `list[Statement]` | All statements (file/class/function-scoped), flat |
| `metadata` | `dict \| None` | Config files only |

### Function

Source: `src/breezeai_cog/schemas/capture.py:114`

| Field | Type | Notes |
|---|---|---|
| `id` | `str` | |
| `parentId` | `str` | Links to containing File or Class |
| `name` | `str` | |
| `type` | `str` | `method`/`function`/`constructor`/`arrow_function`/... (open string) |
| `startLine`, `endLine` | `int` | |
| `path`, `visibility`, `isStatic`, `isAsync` | optional | `isAsync` is present only when true |
| `generics`, `returnType`, `receiverType` | `str \| None` | |
| `params` | `list[Parameter]` | `Parameter` = `{name, type, decorators, default?}` (`capture.py:42`) |
| `decorators` | `list[Decorator]` | |
| `uiRole` | `str \| None` | For function-anchored components/hooks |
| `metadata` | `dict \| None` | |
| `calls` | `list[Call]` | `{name, path?}` — path builds CALLS edges |

### Class

Source: `src/breezeai_cog/schemas/capture.py:144`
Open node (`extra="allow"`).

| Field | Type | Notes |
|---|---|---|
| `id`, `parentId`, `name`, `startLine`, `endLine` | required | |
| `type` | `ClassType` | enum — see below |
| `path`, `visibility`, `isAbstract`, `isSealed` | optional | `isSealed` is present only when true |
| `generics`, `extends` | `str \| None` | `extends` → EXTENDS edge |
| `alias` | `str \| None` | The type's other name from the same declaration: the tag of a C/C++ `typedef struct _tag { ... } name;` when it differs from `name`; never an alias declared elsewhere |
| `implements` | `list[str]` | |
| `constructorParams` | `list[ConstructorParam]` | `{name, type}` |
| `decorators` | `list[Decorator]` | |
| `uiRole` | `str \| None` | |
| `metadata` | `dict \| None` | |

### Statement

Source: `src/breezeai_cog/schemas/capture.py:62`

| Field | Type | Notes |
|---|---|---|
| `id`, `parentId`, `nodeType`, `text`, `startLine`, `endLine` | required | |
| `semanticType` | `SemanticType \| None` | Set only on route/db/api/event detection |
| `path`, `name`, `decorators`, `uiRole` | optional | |
| `isPartial` | `bool \| None` | True on each part of a split oversized statement |
| Route-only | `framework`, `method`, `endpoint`, `handler`, `handlerLine`, `handlerPath`, `routeKind`, `isRegex`, `version`, `authRequired`, `guards`, `requestDTO`, `responseDTO`, `dataLoaders` | all optional |
| DB-only | `dataAccessHint` | optional |
| GraphQL | `keyFields` | optional |
| IaC | `platform` | optional |

## Enums

Source: `src/breezeai_cog/schemas/enums.py` (`Literal` aliases, so they validate in Pydantic and round-trip into JSON Schema)

**FileType** (`enums.py:12`): `code`, `config`

**ClassType** (`enums.py:15`): `class`, `interface`, `struct`, `union`, `record`, `enum`, `module`, `trait`

**SemanticType** (`enums.py:28`), also allowed as `null` on `Statement.semanticType`:
- Behaviour: `route`, `api_call`, `db_method_call`, `query_statement`, `eventbus_send`, `eventbus_publish`, `eventbus_consumer`, `verticle_deploy`, `service_proxy`, `timer`, `graphql_entity`
- Document or declaration roles: `structured_data`, `comment`, `enum_member`, `data_model`
- IaC: `iac_resource`, `iac_data`, `iac_module`, `iac_variable_value`

## JSON Schema generation

Source: `src/breezeai_cog/schemas/version.py:23`

```python
from breezeai_cog.schemas import export_json_schema
schema = export_json_schema()
# or: breezeai-cog schema --out capture-schema.json
```

Schema is a `oneOf [ProjectMetaData, FileRecord]` under `$schema: 2020-12`. Never hand-edit; always regenerate from Pydantic models.
