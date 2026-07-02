"""Pydantic models for the code-capture NDJSON contract (Part C).

These Pydantic models are the **SOURCE OF TRUTH** for the capture contract (Part C).
A language-agnostic JSON Schema is *generated* from them
(``breezeai_cog.schemas.export_json_schema``) for cross-language consumers such as
the Node backend — a build artifact, never hand-edited. ``SCHEMA_VERSION`` tracks
the contract version.

Conventions
-----------
* Field names mirror the JSON keys **exactly** (camelCase) so emitted NDJSON
  matches the contract byte-for-byte. The only exception is ``__type`` (Python
  forbids dunder field names), aliased on :class:`ProjectMetaData`.
* Relations are capture-only: ``id`` on every record, ``parentId`` on every child
  (containment → HAS_FUNCTION / HAS_CLASS / HAS_METHOD / HAS_STATEMENT), plus
  ``importFiles`` / ``calls[].path`` / ``extends`` (cross-file). The emitter
  assigns ``id``/``parentId``; parsers build the nested tree.
* ``File`` and ``Class`` are **open** nodes (``extra="allow"`` — custom primitive
  attributes are preserved); ``Function``/``Statement`` are allow-listed.
* Serialize with ``model_dump_json(by_alias=True, exclude_none=True)`` so
  ``__type`` emits correctly and route-/db-only fields (default ``None``) stay
  **absent** unless the parser populated them. Collection fields default to ``[]``
  for parser ergonomics and may appear empty in output (schema-valid).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import ClassType, FileType, SemanticType


class Decorator(BaseModel):
    """Annotation ``{name, args}`` — same shape on function / class / param."""

    name: str
    args: list[str] = Field(default_factory=list)


class Parameter(BaseModel):
    name: str
    type: str
    decorators: list[Decorator] = Field(default_factory=list)
    default: str | None = None  # default-value expr, e.g. FastAPI `Depends(get_db)`


class ConstructorParam(BaseModel):
    """``class.constructorParams[]`` item — ``{name, type}`` only."""

    name: str
    type: str


class Call(BaseModel):
    name: str
    # callee's in-repo file path → builds CALLS; null / omitted for external.
    path: str | None = None


class Statement(BaseModel):
    """Flat statement. Two-axis type: ``nodeType`` (structural, always present)
    + ``semanticType`` (set only on route/db/api/event detection)."""

    # required
    id: str
    parentId: str
    nodeType: str
    text: str
    startLine: int
    endLine: int
    # common optional
    semanticType: SemanticType | None = None
    path: str | None = None
    name: str | None = None  # declared name (declaration node types)
    # route-only
    framework: str | None = None
    method: str | None = None  # HTTP verb (route/api_call) or db method (db_method_call)
    endpoint: str | None = None  # route path (route) or outbound URL (api_call)
    handler: str | None = None
    handlerLine: int | None = None
    routeKind: str | None = None
    isRegex: bool | None = None
    version: str | None = None
    authRequired: bool | None = None
    guards: list[str] | None = None
    requestDTO: str | None = None
    responseDTO: str | None = None
    dataLoaders: list[str] | None = None
    # db_method_call-only
    dataAccessHint: str | None = None


class Function(BaseModel):
    # required
    id: str
    parentId: str
    name: str
    type: str  # kind: method/function/constructor/arrow_function/... (open string)
    startLine: int
    endLine: int
    # optional
    path: str | None = None
    visibility: str | None = None
    isStatic: bool | None = None
    generics: str | None = None
    params: list[Parameter] = Field(default_factory=list)
    decorators: list[Decorator] = Field(default_factory=list)
    returnType: str | None = None
    metadata: dict[str, Any] | None = None
    calls: list[Call] = Field(default_factory=list)
    # Statements are NOT nested here — they live flat on FileRecord.statements and
    # link back via parentId (like methods link to their class). See FileRecord.


class Class(BaseModel):
    model_config = ConfigDict(extra="allow")  # open node — custom attributes preserved

    # required
    id: str
    parentId: str
    name: str
    type: ClassType
    startLine: int
    endLine: int
    # optional
    path: str | None = None
    visibility: str | None = None
    isAbstract: bool | None = None
    generics: str | None = None
    extends: str | None = None  # parent class name → builds EXTENDS
    implements: list[str] = Field(default_factory=list)
    constructorParams: list[ConstructorParam] = Field(default_factory=list)
    decorators: list[Decorator] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None
    # Statements are NOT nested here — see FileRecord.statements (flat, linked via parentId).


class FileRecord(BaseModel):
    """Per-file NDJSON line. ``type`` discriminates code vs config."""

    model_config = ConfigDict(extra="allow")  # open node — custom attributes preserved

    # required
    id: str
    path: str
    type: FileType
    language: str
    loc: int
    # optional
    framework: str | None = None
    importFiles: list[str] = Field(default_factory=list)  # in-repo paths → builds IMPORTS
    externalImports: list[str] = Field(default_factory=list)
    exports: list[str] = Field(default_factory=list)
    functions: list[Function] = Field(default_factory=list)  # code files only; methods link via parentId
    classes: list[Class] = Field(default_factory=list)  # code files only
    # ALL statements (file / class / function-scoped), flat — each links to its owning
    # file/class/function via parentId (HAS_STATEMENT). Not nested inside Function/Class.
    statements: list[Statement] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None  # parsed config metadata (config files only)


class ProjectMetaData(BaseModel):
    """First NDJSON line — repository-level summary (not a graph node)."""

    model_config = ConfigDict(populate_by_name=True)

    # ``__type`` discriminator (dunder field name is illegal in Python → aliased).
    record_type: Literal["projectMetaData"] = Field(
        default="projectMetaData", alias="__type"
    )
    # required
    repositoryName: str
    analyzedLanguages: list[str]
    totalFiles: int
    totalFunctions: int
    totalClasses: int
    totalLinesOfCode: int
    generatedAt: str  # ISO-8601 date-time
    toolVersion: str
    # optional
    repositoryPath: str | None = None
    configs: dict[str, Any] | None = None  # {totalConfigFiles, byType, packageManagers}
