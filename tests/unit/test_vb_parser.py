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


def test_enum_members_captured_as_statements(tmp_path) -> None:
    # Enum members become flat statements parented to the enum Class (queryable text).
    src = b"Enum Status\n  Active = 1\n  Closed\nEnd Enum\n"
    p = tmp_path / "s.vb"
    p.write_bytes(src)
    ctx = ParseContext(path="s.vb", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = VbParser().parse_file(ctx)
    st = next(c for c in rec.classes if c.type == "enum")
    members = [(s.name, s.nodeType) for s in rec.statements if s.parentId == st.id]
    assert members == [("Active", "enum_member"), ("Closed", "enum_member")]
    ctx2 = ParseContext(path="s.vb", abs_path=p, source=src, repo_root=tmp_path,
                        capture_statements=False)
    assert VbParser().parse_file(ctx2).statements == []


def test_catch_finally_clauses_emitted(tmp_path) -> None:
    src = (b"Class X\n Sub M()\n  Try\n   Save()\n  Catch ex As Exception\n"
           b"   Log(ex)\n  Finally\n   Cleanup()\n  End Try\n End Sub\nEnd Class\n")
    p = tmp_path / "e.vb"
    p.write_bytes(src)
    ctx = ParseContext(path="e.vb", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = VbParser().parse_file(ctx)
    node_types = {s.nodeType for s in rec.statements}
    assert {"try_statement", "catch_block", "finally_block"} <= node_types


def test_class_constants_and_fields_captured_as_statements(tmp_path) -> None:
    # Class-level Const / Shared ReadOnly / plain fields become flat statements parented
    # to the Class, with the value preserved in `text`. Heritage clauses (Inherits /
    # Implements), which tree-sitter-vb misparses as field_declaration / ERROR, must NOT
    # leak in as spurious member statements.
    src = (
        b"Public Class Config\n"
        b"    Inherits BaseConfig\n"
        b"    Implements IConfig\n"
        b"    Public Const MaxRetries As Integer = 5\n"
        b"    Public Shared ReadOnly Name As String = \"x\"\n"
        b"    Private counter As Integer\n"
        b"    Public Property Size As Integer\n"
        b"        Get\n"
        b"            Return 42\n"
        b"        End Get\n"
        b"    End Property\n"
        b"End Class\n"
    )
    p = tmp_path / "c.vb"
    p.write_bytes(src)
    ctx = ParseContext(path="c.vb", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = VbParser().parse_file(ctx)
    cls = next(c for c in rec.classes if c.name == "Config")
    members = {s.name: (s.nodeType, (s.text or "").strip()) for s in rec.statements if s.parentId == cls.id}
    assert members["MaxRetries"] == ("const_declaration", "Public Const MaxRetries As Integer = 5")
    assert "Name" in members and members["Name"][0] == "field_declaration"
    assert "counter" in members
    # Heritage misparses must not appear as statements.
    assert "IConfig" not in members and "BaseConfig" not in members
    # Property accessor bodies must NOT be pulled up and mis-parented to the class.
    class_texts = [t for _, t in members.values()]
    assert not any("Return 42" in t for t in class_texts), "property body leaked into class"
    # Nothing captured without the flag.
    ctx2 = ParseContext(path="c.vb", abs_path=p, source=src, repo_root=tmp_path,
                        capture_statements=False)
    assert VbParser().parse_file(ctx2).statements == []
