"""Contract tests for the Part C capture schema (Pydantic = source of truth).

Constructs representative records (Pydantic validates on construction), checks the
serialized JSON against the *generated* per-model JSON Schema, and asserts the
serialization invariants the emitter relies on.
"""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog.schemas import (
    Call,
    Class,
    ConstructorParam,
    Decorator,
    FileRecord,
    Function,
    Parameter,
    ProjectMetaData,
    SCHEMA_VERSION,
    Statement,
    export_json_schema,
)


def _dump(model):
    return json.loads(model.model_dump_json(by_alias=True, exclude_none=True))


def _validate(instance, model):
    schema = model.model_json_schema(by_alias=True)
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=str)
    assert not errors, "\n".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


@pytest.fixture
def project_meta() -> ProjectMetaData:
    return ProjectMetaData(
        repositoryName="my-app",
        analyzedLanguages=["typescript", "python"],
        totalFiles=376,
        totalFunctions=1582,
        totalClasses=428,
        totalLinesOfCode=98838,
        generatedAt="2026-06-29T10:30:00Z",
        toolVersion="0.0.0",
        repositoryPath="/work/my-app",
        configs={"totalConfigFiles": 12, "byType": {}, "packageManagers": ["npm"]},
    )


@pytest.fixture
def file_record() -> FileRecord:
    route = Statement(
        id="src/order.controller.ts:16:2",
        parentId="src/order.controller.ts#OrderController#getOrder@15",
        nodeType="decorator",
        semanticType="route",
        text="@Get(':id')",
        startLine=16,
        endLine=16,
        method="GET",
        endpoint="/orders/:id",
        framework="nestjs",
        handler="getOrder",
        routeKind="route",
        guards=["JwtAuthGuard"],
        responseDTO="Order",
    )
    fn = Function(
        id="src/order.controller.ts#OrderController#getOrder@15",
        parentId="src/order.controller.ts#OrderController",
        name="getOrder",
        type="method",
        startLine=15,
        endLine=22,
        visibility="public",
        isStatic=False,
        params=[Parameter(name="id", type="string", decorators=[Decorator(name="Param", args=["id"])])],
        calls=[Call(name="findById", path="src/order.repo.ts")],
        returnType="Promise<Order>",
        statements=[route],
    )
    cls = Class(
        id="src/order.controller.ts#OrderController",
        parentId="src/order.controller.ts",
        name="OrderController",
        type="class",
        startLine=10,
        endLine=80,
        visibility="public",
        decorators=[Decorator(name="Controller", args=["orders"])],
        constructorParams=[ConstructorParam(name="repo", type="OrderRepo")],
        customTag="x",  # open node — arbitrary primitive attribute
    )
    return FileRecord(
        id="src/order.controller.ts",
        path="src/order.controller.ts",
        type="code",
        language="typescript",
        loc=80,
        framework="nestjs",
        importFiles=["src/order.repo.ts"],
        externalImports=["@nestjs/common"],
        exports=["OrderController"],
        functions=[fn],
        classes=[cls],
    )


def test_schema_version() -> None:
    assert SCHEMA_VERSION == "2.0"


def test_project_metadata_valid(project_meta: ProjectMetaData) -> None:
    data = _dump(project_meta)
    assert data["__type"] == "projectMetaData"  # discriminator survives serialization
    _validate(data, ProjectMetaData)


def test_file_record_nested_valid(file_record: FileRecord) -> None:
    _validate(_dump(file_record), FileRecord)


def test_plain_statement_omits_route_only_fields() -> None:
    plain = _dump(
        Statement(id="f.py:3:0", parentId="f.py", nodeType="if_statement",
                  text="if x:", startLine=3, endLine=5)
    )
    assert not ({"method", "endpoint", "guards", "semanticType", "handler"} & set(plain))
    assert set(plain) == {"id", "parentId", "nodeType", "text", "startLine", "endLine"}


def test_open_node_preserves_extra(file_record: FileRecord) -> None:
    assert _dump(file_record)["classes"][0]["customTag"] == "x"


def test_generated_json_schema_is_valid() -> None:
    Draft202012Validator.check_schema(ProjectMetaData.model_json_schema(by_alias=True))
    Draft202012Validator.check_schema(FileRecord.model_json_schema(by_alias=True))
    combined = export_json_schema()
    assert combined["x-version"] == SCHEMA_VERSION
    assert "oneOf" in combined and len(combined["oneOf"]) == 2
