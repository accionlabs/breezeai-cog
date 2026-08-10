"""Groovy parser extraction tests + FQCN import resolution + schema validation.

Groovy is a best-effort language (dekobon grammar): the package/class/method/field/enum
skeleton is reliable, expression-body detail may degrade to missing nodes. These tests
assert the skeleton + resolution the graph depends on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.groovy.parser import GroovyParser
from breezeai_cog.schemas import ConstructorParam, FileRecord

SRC = b'''package com.acme.orders

import java.util.List
import com.acme.repo.OrderRepo
import groovy.transform.CompileStatic

@CompileStatic
class OrderController extends Base implements IController, Serializable {
    private final OrderRepo repo
    public static int MAX = 5

    OrderController(OrderRepo repo) { this.repo = repo }

    Order getOrder(Long id) {
        return repo.findById(id)
    }

    static void ping() {}
}

interface IController { String describe() }

enum Status { OPEN, CLOSED }

def scriptHelper(x) { return x + 1 }
'''

REL = "src/main/groovy/com/acme/orders/OrderController.groovy"


def _parse(tmp_path, *, capture=False) -> FileRecord:
    repo_dir = tmp_path / "src/main/groovy/com/acme/repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "OrderRepo.groovy").write_text("package com.acme.repo\ninterface OrderRepo {}\n")
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(SRC)
    parser = GroovyParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.groovy")))
    ctx = ParseContext(path=REL, abs_path=p, source=SRC, repo_root=tmp_path,
                       resolution_index=index, capture_statements=capture)
    return parser.parse_file(ctx)


def _parse_src(tmp_path, src: bytes, *, capture=False) -> FileRecord:
    p = tmp_path / "T.groovy"
    p.write_bytes(src)
    ctx = ParseContext(path="T.groovy", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=capture)
    return GroovyParser().parse_file(ctx)


def _grammar_supports_nested() -> bool:
    """True when the loaded Groovy grammar captures nested (inner) types — i.e. the patched
    fork is installed. Stock dekobon cannot, so nesting-dependent tests skip there."""
    src = b"class A { class B {} }"
    ctx = ParseContext(path="A.groovy", abs_path=Path("A.groovy"), source=src, repo_root=Path("."))
    rec = GroovyParser().parse_file(ctx)
    return "B" in {c.name for c in rec.classes}


requires_patched_grammar = pytest.mark.skipif(
    not _grammar_supports_nested(),
    reason="requires the patched Groovy grammar (nested types / modifier enums); stock dekobon lacks it",
)


def test_imports_and_fqcn_resolution(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert rec.language == "groovy"
    assert "java.util.List" in rec.externalImports
    assert any(p.endswith("com/acme/repo/OrderRepo.groovy") for p in rec.importFiles)  # FQCN resolved


def test_types_and_heritage(tmp_path) -> None:
    rec = _parse(tmp_path)
    by_name = {c.name: c for c in rec.classes}
    assert by_name["OrderController"].type == "class"
    assert by_name["IController"].type == "interface"
    assert by_name["Status"].type == "enum"
    ctrl = by_name["OrderController"]
    assert ctrl.extends == "Base" and ctrl.implements == ["IController", "Serializable"]
    assert {d.name for d in ctrl.decorators} == {"CompileStatic"}
    assert ctrl.constructorParams == [ConstructorParam(name="repo", type="OrderRepo")]
    assert ctrl.visibility == "public" and ctrl.isAbstract is False
    assert by_name["IController"].isAbstract is True  # interfaces are abstract


def test_methods(tmp_path) -> None:
    rec = _parse(tmp_path)
    get = next(f for f in rec.functions if f.name == "getOrder")
    assert get.type == "method" and get.visibility == "public" and get.returnType == "Order"
    assert get.params[0].name == "id" and get.params[0].type == "Long"
    assert "findById" in [c.name for c in get.calls]
    ctrl = next(c for c in rec.classes if c.name == "OrderController")
    assert get.parentId == ctrl.id  # HAS_METHOD wiring
    assert any(f.type == "constructor" for f in rec.functions)
    ping = next(f for f in rec.functions if f.name == "ping")
    assert ping.isStatic is True


def test_top_level_script_method(tmp_path) -> None:
    # Groovy allows methods outside a class — attributed to the file, not dropped.
    rec = _parse(tmp_path)
    helper = next(f for f in rec.functions if f.name == "scriptHelper")
    assert helper.parentId == rec.id


def test_statements_and_detection(tmp_path) -> None:
    assert _parse(tmp_path, capture=False).statements == []
    rec = _parse(tmp_path, capture=True)
    db = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert db and db[0].dataAccessHint  # repo.findById(...) detected as a DB call


def test_trait_mapped_to_trait_type(tmp_path) -> None:
    # A trait gets its own `trait` ClassType; a class still uses it via `implements`.
    src = b"trait Reversible { def reverse() {} }\nclass Sentence implements Reversible {}\n"
    p = tmp_path / "T.groovy"
    p.write_bytes(src)
    ctx = ParseContext(path="T.groovy", abs_path=p, source=src, repo_root=tmp_path)
    rec = GroovyParser().parse_file(ctx)
    by_name = {c.name: c for c in rec.classes}
    assert by_name["Reversible"].type == "trait"
    assert by_name["Sentence"].implements == ["Reversible"]
    assert "reverse" in {f.name for f in rec.functions}


def test_closure_calls_fold_into_enclosing_method(tmp_path) -> None:
    # A closure's calls attribute to the nearest named enclosing method, not dropped.
    src = (
        "class Outer {\n"
        "  void run() { [1].each { o -> handle(o) } }\n"
        "}\n"
    ).encode()
    p = tmp_path / "Outer.groovy"
    p.write_bytes(src)
    ctx = ParseContext(path="Outer.groovy", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = GroovyParser().parse_file(ctx)
    assert "handle" in {c.name for f in rec.functions if f.name == "run" for c in f.calls}


def test_nested_type_no_fabrication(tmp_path) -> None:
    # Invariant that holds under BOTH grammars: the outer class is captured and the parser
    # NEVER fabricates a class node from a misparse (no keyword-named or bogus class).
    # Absent > wrong. (The positive nested-capture assertion lives in the gated test below.)
    src = (
        "class Outer {\n"
        "  static class Inner { void innerMethod() {} }\n"
        "  void run() {}\n"
        "}\n"
    ).encode()
    rec = _parse_src(tmp_path, src)
    names = {c.name for c in rec.classes}
    assert "Outer" in names
    assert not (names & {"class", "enum", "interface", "trait"})  # no fabricated keyword-class


@requires_patched_grammar
def test_nested_type_captured(tmp_path) -> None:
    # With the patched grammar the inner type is a real Class parented to the outer, the
    # outer's members before/after it are correctly attributed, and the outer span reaches
    # its true closing brace (no truncation).
    src = (
        "class Outer {\n"                       # line 1
        "  void run() {}\n"                     # 2
        "  static class Inner {\n"              # 3
        "    void innerMethod() {}\n"           # 4
        "  }\n"                                 # 5
        "  void after() {}\n"                   # 6
        "}\n"                                   # 7
    ).encode()
    rec = _parse_src(tmp_path, src)
    outer = next(c for c in rec.classes if c.name == "Outer")
    inner = next(c for c in rec.classes if c.name == "Inner")
    assert inner.parentId == outer.id  # nested, not hoisted to the file
    assert outer.endLine == 7  # outer span not truncated at the nested type
    innerm = next(f for f in rec.functions if f.name == "innerMethod")
    assert innerm.parentId == inner.id  # inner method parented to Inner, not Outer
    after = next(f for f in rec.functions if f.name == "after")
    assert after.parentId == outer.id  # member after the nested type still on Outer, not orphaned


def test_endpoint_concatenation(tmp_path) -> None:
    src = b'class C { void m(String id){ httpClient.get("/users/" + id) } }'
    p = tmp_path / "C.groovy"
    p.write_bytes(src)
    ctx = ParseContext(path="C.groovy", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = GroovyParser().parse_file(ctx)
    assert any(s.semanticType == "api_call" and s.endpoint == "/users/{id}" for s in rec.statements)


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, capture=True)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_corrupt_declaration_not_emitted_as_method(tmp_path) -> None:
    # False-positive guard: a parenthesised-constant enum body makes the grammar merge the
    # following field + method into one garbled method_declaration (a field `ID` would surface
    # as a method `ID(...)`). Such a corrupt-header declaration must be SKIPPED, not emitted —
    # a fabricated method is high-confidence wrong data. Absent beats wrong.
    src = (
        "class LibraryInfo {\n"
        "  enum Kind { A(1), B(2) }\n"
        "  public int ID\n"
        "  boolean isPrelims(Integer x) { return x > 0 }\n"
        "  String contentSet() { return 'x' }\n"
        "}\n"
    ).encode()
    p = tmp_path / "LibraryInfo.groovy"
    p.write_bytes(src)
    ctx = ParseContext(path="LibraryInfo.groovy", abs_path=p, source=src, repo_root=tmp_path)
    rec = GroovyParser().parse_file(ctx)
    names = {f.name for f in rec.functions}
    assert "ID" not in names  # the field must NOT become a method
    assert "LibraryInfo" in {c.name for c in rec.classes}  # class skeleton still recovered


def test_messy_body_method_still_captured(tmp_path) -> None:
    # Counterpart to the guard: an error nested inside a method *body* (named-arg commas) must
    # NOT suppress the method — the header is trustworthy, only the body detail degrades.
    src = b"class C { int ok(){ new Foo(a: 1, b: 2); return 1 } }\n"
    p = tmp_path / "C.groovy"
    p.write_bytes(src)
    ctx = ParseContext(path="C.groovy", abs_path=p, source=src, repo_root=tmp_path)
    rec = GroovyParser().parse_file(ctx)
    assert "ok" in {f.name for f in rec.functions}


def test_no_wrong_call_edge_on_ambiguous_or_external(tmp_path) -> None:
    # Edges are honest-null: a call whose receiver type is unknown/external resolves to None,
    # never a guessed file. Absent edge > wrong edge.
    src = b"package m\nclass A { void run(){ someGlobal.doThing(); helper.save('x') } }\n"
    p = tmp_path / "m" / "A.groovy"
    p.parent.mkdir(parents=True)
    p.write_bytes(src)
    ctx = ParseContext(path="m/A.groovy", abs_path=p, source=src, repo_root=tmp_path)
    rec = GroovyParser().parse_file(ctx)
    run = next(f for f in rec.functions if f.name == "run")
    assert all(c.path is None for c in run.calls)


def test_degraded_file_keeps_class_skeleton(tmp_path) -> None:
    # Regression: a parenthesised-enum-constant body degrades to comma-errors (the grammar's
    # known weak spot), but the enclosing class name + its plain methods must still be
    # recovered, and nothing may be fabricated. Absent > wrong.
    src = (
        "class LibraryInfo {\n"
        "  public int ID\n"
        "  boolean isPrelims(Integer x) { return x > 0 }\n"
        "  enum Kind { A(1), B(2), C(3) }\n"  # this body is where the grammar stumbles
        "  String contentSet() { return 'x' }\n"
        "}\n"
    ).encode()
    p = tmp_path / "LibraryInfo.groovy"
    p.write_bytes(src)
    ctx = ParseContext(path="LibraryInfo.groovy", abs_path=p, source=src, repo_root=tmp_path)
    rec = GroovyParser().parse_file(ctx)
    assert any(c.name == "LibraryInfo" for c in rec.classes)
    # Methods declared before the degraded enum body are reliably recovered.
    assert "isPrelims" in {f.name for f in rec.functions}
    # No fabricated keyword-named class from the misparsed enum.
    assert not ({c.name for c in rec.classes} & {"class", "enum", "interface", "trait"})


def test_same_package_call_resolves_without_import(tmp_path) -> None:
    # A same-package class needs no import; `RestCommon.dataGet()` must still resolve
    # cross-file via the FQCN index (seeded into the call-resolution bindings).
    (tmp_path / "RestCommon.groovy").write_text(
        "package a.b\nclass RestCommon { static Map dataGet(String u) { return [:] } }\n"
    )
    main = tmp_path / "Action.groovy"
    main.write_text("package a.b\nclass Action { def run() { RestCommon.dataGet('/x') } }\n")
    parser = GroovyParser()
    idx = parser.build_index(tmp_path, list(tmp_path.rglob("*.groovy")))
    rec = parser.parse_file(ParseContext(path="Action.groovy", abs_path=main,
                                         source=main.read_bytes(), repo_root=tmp_path,
                                         resolution_index=idx, capture_statements=True))
    call = next(c for f in rec.functions for c in f.calls if c.name == "dataGet")
    assert call.path == "RestCommon.groovy"


# --- enum constant VALUE + doc capture (BREEZEAI-943) ---

def test_enum_constant_values_and_doc(tmp_path) -> None:
    src = (
        "enum Priority {\n"
        "  LOW('1'),    // lowest\n"
        "  /** the peak */\n"
        "  HIGH('3')\n"
        "}\n"
    ).encode()
    rec = _parse_src(tmp_path, src)
    enum = next(c for c in rec.classes if c.type == "enum")
    assert (enum.metadata or {}).get("constants") == [
        {"name": "LOW", "value": "1", "doc": "lowest"},   # trailing same-line comment
        {"name": "HIGH", "value": "3", "doc": "the peak"},  # preceding groovydoc
    ]


def test_enum_constant_no_arg_and_non_literal_are_honest_null(tmp_path) -> None:
    rec = _parse_src(tmp_path, b"enum E { A, B(compute()) }")
    enum = next(c for c in rec.classes if c.type == "enum")
    assert (enum.metadata or {}).get("constants") == [
        {"name": "A", "value": None, "doc": None},   # no argument
        {"name": "B", "value": None, "doc": None},   # non-literal argument → honest-null
    ]


def test_enum_constants_emit_no_statements(tmp_path) -> None:
    # Constants are vocabulary carried on metadata, never emitted as statements.
    rec = _parse_src(tmp_path, b"enum E { A('1'), B('2') }", capture=True)
    assert rec.statements == []


def test_plain_class_metadata_stays_none(tmp_path) -> None:
    rec = _parse_src(tmp_path, b"class C { int x }")
    assert next(c for c in rec.classes if c.name == "C").metadata is None


@requires_patched_grammar
def test_modifier_and_nested_enum_constants(tmp_path) -> None:
    # The load-bearing real-world shape: a modifier-prefixed enum nested in a class, whose
    # members follow the constants without a `;`. Grammar fix + capture together recover the
    # constant VALUES that previously landed nowhere.
    src = (
        "class Holder {\n"
        "  public enum Entry {\n"
        "    APPLY_STATUS('CAPLC09'),\n"
        "    APPLY_DATE('CAPLC11')\n"
        "    private final String col\n"
        "    Entry(String col) { this.col = col }\n"
        "  }\n"
        "  def use() {}\n"
        "}\n"
    ).encode()
    rec = _parse_src(tmp_path, src)
    holder = next(c for c in rec.classes if c.name == "Holder")
    entry = next(c for c in rec.classes if c.name == "Entry")
    assert entry.type == "enum" and entry.parentId == holder.id
    assert (entry.metadata or {}).get("constants") == [
        {"name": "APPLY_STATUS", "value": "CAPLC09", "doc": None},
        {"name": "APPLY_DATE", "value": "CAPLC11", "doc": None},
    ]
