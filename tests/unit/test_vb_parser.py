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


# The grammar has no rule for a nested type: `Public Class Result` inside a class surfaces as a
# field_declaration, the enclosing class_block ends at the *nested* End Class, and the members
# after it fall into an ERROR node. The outer types are parsed with the nested ones blanked, and
# each nesting level on its own.
NESTED_TYPE = b'''Namespace Shop
  Public Class OrderService
    Public Sub Validate()
    End Sub

    \' The outcome of a save.
    <Serializable>
    Public Class Result
      Public Sub Describe()
      End Sub

      Public Enum Kind
        Ok
      End Enum
    End Class

    Public Sub Save()
    End Sub
  End Class
End Namespace
'''


def _parse_source(tmp_path, src: bytes) -> FileRecord:
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src)
    ctx = ParseContext(path=REL, abs_path=p, source=src, repo_root=tmp_path, capture_statements=True)
    return VbParser().parse_file(ctx)


def test_nested_types_and_the_members_after_them_are_captured(tmp_path) -> None:
    rec = _parse_source(tmp_path, NESTED_TYPE)
    classes = {c.id.split("#", 1)[1]: (c.name, c.startLine, c.endLine, c.parentId) for c in rec.classes}
    fid = rec.id
    assert classes == {
        "OrderService": ("OrderService", 2, 19, fid),              # ends at its own End Class
        "OrderService.Result": ("Result", 8, 15, fid),
        "OrderService.Result.Kind": ("Kind", 12, 14, fid),
    }
    result = next(c for c in rec.classes if c.name == "Result")
    assert [d.name for d in result.decorators] == ["Serializable"]
    owners = {f.name: f.parentId.split("#", 1)[1] for f in rec.functions}
    assert owners == {"Validate": "OrderService", "Save": "OrderService",
                      "Describe": "OrderService.Result"}
    # The nested declaration line is not a field of the outer class.
    assert not any("Public Class Result" in s.text for s in rec.statements)
    # Its comment belongs to the nested type, not to the next outer member.
    comment = next(s for s in rec.statements if "outcome of a save" in s.text)
    assert comment.parentId.endswith("#OrderService.Result")


def test_unpaired_type_lines_keep_the_old_cut_off(tmp_path) -> None:
    # An End Class with no opener: the layout can't be trusted, so the file is parsed as written
    # and members after the nested type are dropped rather than mis-parented.
    src = NESTED_TYPE.replace(b"  End Class\nEnd Namespace", b"  End Class\n  End Class\nEnd Namespace")
    rec = _parse_source(tmp_path, src)
    assert "Describe" not in {f.name for f in rec.functions}
    assert all(f.parentId.endswith("#OrderService") for f in rec.functions)


def test_a_class_without_a_nested_type_is_unaffected(tmp_path) -> None:
    src = b'''Namespace Shop
  Public Class OrderService
    Public Sub Validate()
    End Sub
    Public Function Total() As Integer
      Return 1
    End Function
  End Class
End Namespace
'''
    rec = _parse_source(tmp_path, src)
    assert {f.name for f in rec.functions} == {"Validate", "Total"}


def test_spans_end_on_the_closing_line(tmp_path) -> None:
    # VB block nodes include the trailing newline, so tree-sitter ends them at column 0 of the
    # next line; endLine must still be the line of ``End Function`` / ``End Class``.
    rec = _parse(tmp_path, capture=True)
    ctrl = next(c for c in rec.classes if c.name == "OrderController")
    assert (ctrl.startLine, ctrl.endLine) == (7, 22)
    get = next(f for f in rec.functions if f.name == "GetOrder")
    assert (get.startLine, get.endLine) == (17, 21)
    ret = next(s for s in rec.statements if s.nodeType == "return_statement")
    assert (ret.startLine, ret.endLine) == (20, 20)


def test_attribute_args_as_written(tmp_path) -> None:
    src = b'''Public Class P
    <X("a""b", Roles:="Admin", "s" & "t", Order.Last, y := 2)>
    Public Sub M()
    End Sub
End Class
'''
    p = tmp_path / "P.vb"
    p.write_bytes(src)
    rec = VbParser().parse_file(ParseContext(path="P.vb", abs_path=p, source=src, repo_root=tmp_path))
    m = next(f for f in rec.functions if f.name == "M")
    # a lone string literal unquoted; a named arg, a concatenation or a member as written
    assert m.decorators[0].args == ['a"b', 'Roles:="Admin"', '"s" & "t"', "Order.Last", "y := 2"]


def test_stacked_attribute_lines_are_joined_in_place() -> None:
    from breezeai_cog.parsers.vb.source import join_attribute_blocks

    src = b'    <HttpPost>\r\n    <Route("create")>\r\n    Public Function Create() As Object\r\n'
    out = join_attribute_blocks(src)
    assert out == b'    <HttpPost,\r\n     Route("create")>\r\n    Public Function Create() As Object\r\n'
    assert len(out) == len(src)


def test_attribute_join_leaves_other_shapes_alone() -> None:
    from breezeai_cog.parsers.vb.source import join_attribute_blocks

    for src in (
        b"<HttpPost>\nPublic Function A() As Object\n",                     # one block: nothing to join
        b"<Assembly: A>\n<Assembly: B>\nPublic Class C\n",                  # assembly targets
        b"<HttpPost> ' post\n<Route(\"x\")>\nPublic Sub M()\n",              # trailing comment
        b"<A>\n<B>\nx = 1\n",                                                # not a declaration
        b"Dim doc = <root>\n<item/>\n<item></item>\nPublic Sub M()\n",       # XML literal tags
        b"<xs:a>\n<xs:b>\nPublic Sub M()\n",                                 # namespaced XML tags
    ):
        assert join_attribute_blocks(src) is src, src


STACKED = b'''Imports System.ServiceModel
Namespace Shop
    <ServiceContract>
    Public Interface IOrderService
        <OperationContract>
        Function GetOrder(id As Integer) As String

        <OperationContract>
        <WebGet(UriTemplate:="orders/{id}")>
        Function GetOrderRest(id As String) As String
    End Interface
End Namespace
'''


def test_stacked_attributes_in_an_interface_are_all_kept(tmp_path) -> None:
    p = tmp_path / "IOrderService.vb"
    p.write_bytes(STACKED)
    ctx = ParseContext(path="IOrderService.vb", abs_path=p, source=STACKED, repo_root=tmp_path)
    rec = VbParser().parse_file(ctx)
    assert [c.name for c in rec.classes] == ["IOrderService"]
    fns = {f.name: f for f in rec.functions}
    assert [d.name for d in fns["GetOrder"].decorators] == ["OperationContract"]
    rest = fns["GetOrderRest"]
    assert [d.name for d in rest.decorators] == ["OperationContract", "WebGet"]
    assert (rest.startLine, rest.endLine) == (8, 10)  # from the first attribute line


BROKEN_HEADER = b'''Public Class ReportsController
    Public Async Function GetRate(<FromQuery> fromX As String, <FromQuery> toY As String) As Task(Of IActionResult)
        If String.IsNullOrWhiteSpace(fromX) OrElse String.IsNullOrWhiteSpace(toY) Then
            Return BadRequest()
        End If
        Return Ok()
    End Function

    Public Function Other(x As Integer) As Integer
        Return IsNullOrWhiteSpace(x)
    End Function
End Class
'''


def test_a_broken_method_header_keeps_its_declared_name(tmp_path) -> None:
    # This header shape (an attributed parameter named ``to…``, a generic return type, an
    # ``If … OrElse …`` call condition) makes the grammar fail inside the header; its ``name`` field then points at
    # ``IsNullOrWhiteSpace`` on the If line; the name comes from the header text instead, and the
    # mis-read parameters and return type stay empty rather than wrong.
    p = tmp_path / "R.vb"
    p.write_bytes(BROKEN_HEADER)
    ctx = ParseContext(path="R.vb", abs_path=p, source=BROKEN_HEADER, repo_root=tmp_path)
    rec = VbParser().parse_file(ctx)
    fns = {f.name: f for f in rec.functions}
    assert set(fns) == {"GetRate", "Other"}
    assert fns["GetRate"].params == [] and fns["GetRate"].returnType is None
    assert fns["GetRate"].id == "R.vb#ReportsController#GetRate@2"
    # nothing in the file is named IsNullOrWhiteSpace, so that call resolves to no local function
    assert [c.path for c in fns["Other"].calls if c.name == "IsNullOrWhiteSpace"] == [None]
    assert [p.name for p in fns["Other"].params] == ["x"]  # an intact header is unchanged


_AWAITS = b"""Public Class Orders
    Public Async Function Place(req As Object) As Task
        Await _s3.PutObjectAsync(req)
        Dim r = Await _sqs.ReceiveMessageAsync("orders")
        If r IsNot Nothing Then Await _s3.DeleteObjectAsync(req)
        Return Await _db.SaveAsync(req)
    End Function
End Class
"""


def test_await_is_read_as_a_keyword_and_statements_keep_their_place(tmp_path) -> None:
    from breezeai_cog.parsers.vb.source import parse_vb
    from breezeai_cog.parsers.treesitter import node_text

    # The awaited call parses whole: its receiver is the client, not a variable named Await.
    root = parse_vb(_AWAITS).root_node
    receivers = []
    stack = [root]
    while stack:
        n = stack.pop()
        assert n.type != "ERROR"
        if n.type == "member_access":
            receivers.append(node_text(n.child_by_field_name("object"), _AWAITS))
        stack.extend(n.named_children)
    assert sorted(receivers) == ["_db", "_s3", "_s3", "_sqs"]
    # Ids and text still come from the source as written, Await included.
    rec = _parse_source(tmp_path, _AWAITS)
    stmts = {s.id.split(":", 1)[1]: s.text.strip() for s in rec.statements
             if s.nodeType in ("call_statement", "dim_statement")}
    assert stmts == {
        "3:8": "Await _s3.PutObjectAsync(req)",
        "4:8": 'Dim r = Await _sqs.ReceiveMessageAsync("orders")',
        "5:32": "Await _s3.DeleteObjectAsync(req)",  # the single-line If's statement
    }


def test_await_is_left_alone_where_it_can_be_a_name() -> None:
    from breezeai_cog.parsers.vb.source import blank_await

    kept = [
        b"If Await Is Nothing Then",       # an operator keyword follows
        b"Await = 1",                      # assignment
        b"Dim Await As Integer",           # a declaration
        b"obj.Await x",                    # a member
        b"Await (t)",                      # a call or a parenthesised await: not repaired
        b'x = "Await me" \' Await y',      # in a string, in a comment
        b"REM Await x",
    ]
    for line in kept:
        assert blank_await(line) == line, line
    assert blank_await(b"    Await _s3.Put()") == b"    Call  _s3.Put()"
    assert blank_await(b"Dim r = Await Get()") == b"Dim r =       Get()"
    assert blank_await(b"If x Then Await Go()") == b"If x Then Call  Go()"


_IMPLEMENTS = b"""Public Class OrderService
    Implements IOrderService
    Public Async Function GetByIdAsync(id As Integer) As Task(Of OrderDto) Implements IOrderService.GetByIdAsync
        Return Nothing
    End Function
    <Obsolete> Public Function Fetch(id As String) As Order Implements IOrderService.GetOrder, IOther.Get ' both
        Return Nothing
    End Function
    Public ReadOnly Property Name As String Implements IOrderService.Name
        Get
            Return ""
        End Get
    End Property
    Public Sub Save(o As Order) Implements IOrderService.Save
    End Sub
    Public Function GetAll() As List(Of T) Implements IRepository(Of T).GetAll
    End Function
End Class
"""


def test_a_member_implements_clause_is_not_its_return_type(tmp_path) -> None:
    from breezeai_cog.parsers.vb.source import member_implements

    rec = _parse_source(tmp_path, _IMPLEMENTS)
    got = {f.name: (f.returnType, f.startLine) for f in rec.functions}
    assert got["GetByIdAsync"] == ("Task(Of OrderDto)", 3)
    assert got["Fetch"] == ("Order", 6)
    assert got["Save"][0] is None
    assert got["GetAll"][0] == "List(Of T)"
    assert [c.implements for c in rec.classes] == [["IOrderService"]]
    # The clause is read from the source as written.
    assert member_implements(_IMPLEMENTS) == {
        3: ["IOrderService.GetByIdAsync"], 6: ["IOrderService.GetOrder", "IOther.Get"],
        9: ["IOrderService.Name"], 14: ["IOrderService.Save"], 16: ["IRepository(Of T).GetAll"],
    }


def test_implements_is_left_alone_off_a_member_header() -> None:
    from breezeai_cog.parsers.vb.source import blank_implements

    kept = [
        b"    Implements IOrderService",                                 # the class statement
        b'    Dim s = "Function F() Implements I.J"',                     # in a string
        b"    ' Public Function F() As X Implements I.J",                 # a comment
        b"    Public Function F() As X _",                                # continued header
    ]
    for line in kept:
        assert blank_implements(line) == line, line


_GENERICS = b"""Public Class R
    Public Function B() As Task(Of List(Of Customer))
    End Function
    Public Sub S(xs As List(Of Customer), n As Integer)
    End Sub
    Public Function D() As Dictionary(Of String, List(Of Integer))
    End Function
    Public Function E() As Customer
    End Function
End Class
"""


def test_a_generic_type_keeps_its_closing_parentheses(tmp_path) -> None:
    # The grammar reads `Of T` without its parentheses; the `)` it drops are taken from the source.
    rec = _parse_source(tmp_path, _GENERICS)
    got = {f.name: (f.returnType, [(p.name, p.type) for p in f.params]) for f in rec.functions}
    assert got["B"] == ("Task(Of List(Of Customer))", [])
    # A parameter after a generic one is lost to the grammar; a Sub still returns nothing.
    assert got["S"][0] is None and got["S"][1][0] == ("xs", "List(Of Customer)")
    assert got["D"][0] == "Dictionary(Of String, List(Of Integer))"
    assert got["E"][0] == "Customer"
