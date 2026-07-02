"""VB.NET parser extraction tests + statement/detection wiring + schema validation."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.vb.parser import VbParser
from breezeai_cog.schemas import ConstructorParam, FileRecord

SRC = b'''Imports System
Imports Microsoft.AspNetCore.Mvc

Namespace Acme.Orders
    <ApiController>
    <Route("api/orders")>
    Public Class OrderController
        Inherits ControllerBase
        Implements IThing

        Private ReadOnly _repo As IOrderRepo

        Public Sub New(repo As IOrderRepo)
            _repo = repo
        End Sub

        <HttpGet("{id}")>
        Public Function GetOrder(id As Long) As Task(Of Order)
            Dim order = _repo.FindAsync(id)
            Return order
        End Function
    End Class

    Public Interface IThing
    End Interface

    Public Module Helpers
        Public Sub DoIt()
        End Sub
    End Module
End Namespace
'''

REL = "src/Order.vb"


def _parse(tmp_path, *, capture=False) -> FileRecord:
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SRC.decode())
    ctx = ParseContext(path=REL, abs_path=p, source=SRC, repo_root=tmp_path, capture_statements=capture)
    return VbParser().parse_file(ctx)


def test_language_and_imports(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert rec.language == "vb"
    assert "System" in rec.externalImports
    assert "Microsoft.AspNetCore.Mvc" in rec.externalImports


def test_types(tmp_path) -> None:
    rec = _parse(tmp_path)
    by_name = {c.name: c for c in rec.classes}
    assert by_name["OrderController"].type == "class"
    assert by_name["IThing"].type == "interface"
    assert by_name["Helpers"].type == "module"
    ctrl = by_name["OrderController"]
    assert ctrl.extends == "ControllerBase" and ctrl.implements == ["IThing"]  # best-effort heritage
    assert {d.name for d in ctrl.decorators} == {"ApiController", "Route"}
    assert ctrl.constructorParams == [ConstructorParam(name="repo", type="IOrderRepo")]


def test_methods(tmp_path) -> None:
    rec = _parse(tmp_path)
    get = next(f for f in rec.functions if f.name == "GetOrder")
    assert get.type == "method" and get.visibility == "public" and get.returnType == "Task(Of Order)"
    assert [d.name for d in get.decorators] == ["HttpGet"]
    assert get.params[0].name == "id" and get.params[0].type == "Long"
    assert "FindAsync" in [c.name for c in get.calls]
    ctrl = next(c for c in rec.classes if c.name == "OrderController")
    assert get.parentId == ctrl.id
    assert any(f.type == "constructor" and f.name == "New" for f in rec.functions)


def test_statements_and_detection(tmp_path) -> None:
    assert _parse(tmp_path, capture=False).statements == []
    rec = _parse(tmp_path, capture=True)
    db = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert db and db[0].dataAccessHint == "entity_framework"


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, capture=True)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
