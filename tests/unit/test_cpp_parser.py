"""C++ parser extraction tests + local-header import resolution + schema validation.

C++ is a best-effort language (real translation units routinely parse with ``has_error``
from macros / unexpanded preprocessor tokens). These tests assert the class/function/import
skeleton the graph depends on, and — crucially — that a corrupt declaration never seeds a
fabricated node.
"""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.cpp.parser import CppParser
from breezeai_cog.schemas import ConstructorParam, FileRecord

SRC = rb'''
#include <string>
#include "apply.h"

namespace app {

struct Point {
    int x;
    int y;
    double norm() const;
};

class Judge : public Base, private Mix {
public:
    Judge(int seed);
    int decide(const Request& r, int flags = 0);
    void reset();
private:
    int score_;
};

int freeFunc(int a, double b) {
    Judge j(a);
    if (a > 0) { return j.decide(a, b); }
    return compute(a);
}

int Judge::decide(const Request& r, int flags) {
    auto v = r.value();
    return compute(v);
}

}  // namespace app
'''

REL = "src/judge.cpp"


def _parse(tmp_path, *, capture=False) -> FileRecord:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "apply.h").write_text("int compute(int);\n")
    p = tmp_path / REL
    p.write_bytes(SRC)
    parser = CppParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.cpp")) + list(tmp_path.rglob("*.h")))
    ctx = ParseContext(path=REL, abs_path=p, source=SRC, repo_root=tmp_path,
                       resolution_index=index, capture_statements=capture)
    return parser.parse_file(ctx)


def test_language_and_imports(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert rec.language == "cpp"
    assert "<string>" in rec.externalImports  # system include stays external
    assert any(p.endswith("src/apply.h") for p in rec.importFiles)  # local include resolved


def test_class_and_struct_with_heritage(tmp_path) -> None:
    rec = _parse(tmp_path)
    by_name = {c.name: c for c in rec.classes}
    assert by_name["Point"].type == "struct"
    assert by_name["Judge"].type == "class"
    judge = by_name["Judge"]
    assert judge.extends == "Base"  # first base
    assert judge.implements == ["Mix"]  # remaining bases
    assert judge.constructorParams == [ConstructorParam(name="seed", type="int")]


def test_free_function(tmp_path) -> None:
    rec = _parse(tmp_path)
    fn = next(f for f in rec.functions if f.name == "freeFunc")
    assert fn.type == "function" and fn.parentId == rec.id
    assert fn.returnType == "int"
    assert [(p.name, p.type) for p in fn.params] == [("a", "int"), ("b", "double")]
    assert "decide" in {c.name for c in fn.calls} and "compute" in {c.name for c in fn.calls}


def test_inclass_member_function(tmp_path) -> None:
    rec = _parse(tmp_path)
    judge = next(c for c in rec.classes if c.name == "Judge")
    # In-class declaration `int decide(...)` → a flat method attached to the class.
    decls = [f for f in rec.functions if f.name == "decide"]
    assert any(f.parentId == judge.id and f.visibility == "public" for f in decls)
    reset = next(f for f in rec.functions if f.name == "reset")
    assert reset.type == "method" and reset.returnType == "void"
    # The struct's method reads its return type too.
    norm = next(f for f in rec.functions if f.name == "norm")
    assert norm.returnType == "double"


def test_out_of_class_method_attaches_to_class(tmp_path) -> None:
    # `int Judge::decide(...) {...}` (a qualified_identifier declarator) attaches to Judge.
    rec = _parse(tmp_path)
    judge = next(c for c in rec.classes if c.name == "Judge")
    defs = [f for f in rec.functions if f.name == "decide" and f.parentId == judge.id]
    # Both the in-class declaration and the out-of-class definition attach to Judge.
    assert len(defs) == 2
    # The out-of-class definition carries body calls; the declaration does not.
    assert any("compute" in {c.name for c in f.calls} for f in defs)


def test_member_variable_is_not_a_function(tmp_path) -> None:
    rec = _parse(tmp_path)
    names = {f.name for f in rec.functions}
    assert "score_" not in names and "x" not in names and "y" not in names


def test_statements_gated_and_detected(tmp_path) -> None:
    assert _parse(tmp_path, capture=False).statements == []
    rec = _parse(tmp_path, capture=True)
    assert rec.statements  # bodies produce flat statements when capture is on
    # Every statement links to a real function/class/file id.
    ids = {rec.id} | {c.id for c in rec.classes} | {f.id for f in rec.functions}
    assert all(s.parentId in ids for s in rec.statements)


def test_api_call_endpoint_concatenation(tmp_path) -> None:
    src = b'void m(const char* id) { httpClient.get("/users/" + id); }'
    p = tmp_path / "c.cpp"
    p.write_bytes(src)
    ctx = ParseContext(path="c.cpp", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = CppParser().parse_file(ctx)
    assert any(s.semanticType == "api_call" and s.endpoint == "/users/{id}" for s in rec.statements)


def test_malformed_declaration_not_fabricated(tmp_path) -> None:
    # A macro the grammar can't expand (`Q_OBJECT`) makes the enclosing member declaration
    # an ERROR node. Such a corrupt-header member must be SKIPPED, not emitted as a
    # fabricated method — absent beats wrong. The clean method after it is still recovered.
    src = (
        b"class Widget {\n"
        b"    Q_OBJECT\n"
        b"    int brokenSlot(int x);\n"
        b"    int cleanMethod(int y);\n"
        b"};\n"
    )
    p = tmp_path / "w.cpp"
    p.write_bytes(src)
    ctx = ParseContext(path="w.cpp", abs_path=p, source=src, repo_root=tmp_path)
    rec = CppParser().parse_file(ctx)
    assert "Widget" in {c.name for c in rec.classes}  # class skeleton recovered
    assert "brokenSlot" not in {f.name for f in rec.functions}  # corrupt member not fabricated
    assert "cleanMethod" in {f.name for f in rec.functions}  # clean member still captured


def test_unresolved_local_include_is_external(tmp_path) -> None:
    # A local include with no matching repo file resolves to nothing (external), never a
    # fabricated in-repo path.
    src = b'#include "nowhere.h"\nint f() { return 0; }\n'
    p = tmp_path / "x.cpp"
    p.write_bytes(src)
    parser = CppParser()
    idx = parser.build_index(tmp_path, [p])
    ctx = ParseContext(path="x.cpp", abs_path=p, source=src, repo_root=tmp_path, resolution_index=idx)
    rec = parser.parse_file(ctx)
    assert "nowhere.h" in rec.externalImports
    assert rec.importFiles == []


def test_no_wrong_call_edge_when_unresolved(tmp_path) -> None:
    # A call whose target isn't defined in this file resolves to None — never a guessed file.
    src = b"void run() { external.doThing(); alsoExternal(); }\n"
    p = tmp_path / "r.cpp"
    p.write_bytes(src)
    ctx = ParseContext(path="r.cpp", abs_path=p, source=src, repo_root=tmp_path)
    rec = CppParser().parse_file(ctx)
    run = next(f for f in rec.functions if f.name == "run")
    assert all(c.path is None for c in run.calls)


def test_same_file_free_function_call_resolves(tmp_path) -> None:
    src = b"int helper(int a) { return a; }\nint caller() { return helper(1); }\n"
    p = tmp_path / "s.cpp"
    p.write_bytes(src)
    ctx = ParseContext(path="s.cpp", abs_path=p, source=src, repo_root=tmp_path)
    rec = CppParser().parse_file(ctx)
    caller = next(f for f in rec.functions if f.name == "caller")
    call = next(c for c in caller.calls if c.name == "helper")
    assert call.path == "s.cpp"  # same-file definition → resolved to this file


def _parse_with_index(tmp_path, rel: str) -> FileRecord:
    """Parse ``rel`` with a repo-wide index built over every .cpp/.h/.hpp under tmp_path
    (so cross-file call resolution can fire)."""
    parser = CppParser()
    files = (list(tmp_path.rglob("*.cpp")) + list(tmp_path.rglob("*.h"))
             + list(tmp_path.rglob("*.hpp")))
    index = parser.build_index(tmp_path, files)
    p = tmp_path / rel
    return parser.parse_file(ParseContext(path=rel, abs_path=p, source=p.read_bytes(),
                                          repo_root=tmp_path, resolution_index=index))


def test_cross_file_qualified_call_resolves(tmp_path) -> None:
    # An explicit Class::method() call resolves to the file defining that method, and matches
    # regardless of how verbosely the call's scope is written (N::Util::run vs Util::run).
    (tmp_path / "util.cpp").write_bytes(b"int Util::run() { return 1; }\n")
    (tmp_path / "svc.cpp").write_bytes(b"void go() { Util::run(); N::Util::run(); }\n")
    rec = _parse_with_index(tmp_path, "svc.cpp")
    resolved = {c.path for c in _fn(rec, "go").calls if c.name.endswith("run")}
    assert resolved == {"util.cpp"}  # both scope spellings → the defining file


def test_cross_file_free_function_resolves(tmp_path) -> None:
    (tmp_path / "helpers.cpp").write_bytes(b"int MakeId() { return 7; }\n")
    (tmp_path / "svc.cpp").write_bytes(b"int go() { return MakeId(); }\n")
    rec = _parse_with_index(tmp_path, "svc.cpp")
    call = next(c for c in _fn(rec, "go").calls if c.name == "MakeId")
    assert call.path == "helpers.cpp"  # repo-wide free function → its file


def test_cross_file_implicit_this_call_resolves(tmp_path) -> None:
    # A bare Foo() inside a method is an implicit-this call → owner::Foo, resolved even when
    # the method body lives in a different file from the class declaration.
    (tmp_path / "judge.h").write_bytes(b"class Judge { public:\n int Decide();\n int Score();\n};\n")
    (tmp_path / "judge.cpp").write_bytes(
        b'#include "judge.h"\nint Judge::Score() { return 1; }\n'
        b"int Judge::Decide() { return Score(); }\n")
    rec = _parse_with_index(tmp_path, "judge.cpp")
    call = next(c for c in _fn(rec, "Decide").calls if c.name == "Score")
    assert call.path == "judge.cpp"  # owner::Score → same file here (definition lives here)


def test_ambiguous_name_is_honest_null(tmp_path) -> None:
    # A free function defined in TWO files collapses to None — no guessed edge.
    (tmp_path / "a.cpp").write_bytes(b"int Ping() { return 1; }\n")
    (tmp_path / "b.cpp").write_bytes(b"int Ping() { return 2; }\n")
    (tmp_path / "svc.cpp").write_bytes(b"int go() { return Ping(); }\n")
    rec = _parse_with_index(tmp_path, "svc.cpp")
    call = next(c for c in _fn(rec, "go").calls if c.name == "Ping")
    assert call.path is None  # ambiguous across files → honest-null


def test_member_call_typed_by_param(tmp_path) -> None:
    # obj->method() resolves via the receiver's declared type (a param): Dep* d → Dep::Work.
    (tmp_path / "dep.cpp").write_bytes(b"int Dep::Work() { return 1; }\n")
    (tmp_path / "svc.cpp").write_bytes(b"void go(Dep* d) { d->Work(); }\n")
    rec = _parse_with_index(tmp_path, "svc.cpp")
    assert next(c for c in _fn(rec, "go").calls if c.name == "Work").path == "dep.cpp"


def test_member_call_typed_by_smart_pointer(tmp_path) -> None:
    # shared_ptr<Judge> unwraps to Judge; a container (vector) must NOT unwrap.
    (tmp_path / "judge.cpp").write_bytes(b"int Judge::Decide() { return 1; }\n")
    (tmp_path / "svc.cpp").write_bytes(
        b"void go(std::shared_ptr<Judge> j, std::vector<Judge> v) { j->Decide(); v.Decide(); }\n")
    calls = {c.name: c.path for c in _fn(_parse_with_index(tmp_path, "svc.cpp"), "go").calls}
    assert calls["Decide"] == "judge.cpp"  # j (shared_ptr<Judge>) → Judge::Decide; v (vector) → null-keyed


def test_member_call_typed_by_field(tmp_path) -> None:
    # this->dep_->Compute() resolves via the owner class's member-field type.
    (tmp_path / "dep.cpp").write_bytes(b"int Dep::Compute() { return 1; }\n")
    (tmp_path / "svc.h").write_bytes(b"class Svc { Dep* dep_;\n int Run(); };\n")
    (tmp_path / "svc.cpp").write_bytes(
        b'#include "svc.h"\nint Svc::Run() { return this->dep_->Compute(); }\n')
    rec = _parse_with_index(tmp_path, "svc.cpp")
    assert next(c for c in _fn(rec, "Run").calls if c.name == "Compute").path == "dep.cpp"


def test_auto_make_shared_resolves(tmp_path) -> None:
    (tmp_path / "judge.cpp").write_bytes(b"int Judge::Decide() { return 1; }\n")
    (tmp_path / "svc.cpp").write_bytes(
        b"void go() { auto j = std::make_shared<Judge>(); j->Decide(); }\n")
    rec = _parse_with_index(tmp_path, "svc.cpp")
    assert next(c for c in _fn(rec, "go").calls if c.name == "Decide").path == "judge.cpp"


def test_auto_from_unknown_return_is_null(tmp_path) -> None:
    # auto from a call whose return type we cannot see → no type, no edge (not a guess).
    (tmp_path / "judge.cpp").write_bytes(b"int Judge::Decide() { return 1; }\n")
    (tmp_path / "svc.cpp").write_bytes(
        b"void go() { auto j = Factory::Create(); j->Decide(); }\n")
    rec = _parse_with_index(tmp_path, "svc.cpp")
    assert next(c for c in _fn(rec, "go").calls if c.name == "Decide").path is None


def test_reassigned_receiver_is_honest_null(tmp_path) -> None:
    # Guard: a variable reassigned after its declaration has an untrustworthy type → null.
    (tmp_path / "foo.cpp").write_bytes(b"int Foo::Run() { return 1; }\n")
    (tmp_path / "svc.cpp").write_bytes(
        b"void go(Foo* f, Foo* g) { f = g; f->Run(); }\n")
    rec = _parse_with_index(tmp_path, "svc.cpp")
    assert next(c for c in _fn(rec, "go").calls if c.name == "Run").path is None


def test_inherited_method_call_resolves(tmp_path) -> None:
    # A bare call to a method the owner INHERITS resolves to the base class's file.
    (tmp_path / "base.h").write_bytes(b"class Base { public: int Shared(); };\n")
    (tmp_path / "base.cpp").write_bytes(b'#include "base.h"\nint Base::Shared() { return 1; }\n')
    (tmp_path / "derived.h").write_bytes(b'#include "base.h"\nclass Derived : public Base { int Run(); };\n')
    (tmp_path / "derived.cpp").write_bytes(
        b'#include "derived.h"\nint Derived::Run() { return Shared(); }\n')
    rec = _parse_with_index(tmp_path, "derived.cpp")
    assert next(c for c in _fn(rec, "Run").calls if c.name == "Shared").path == "base.cpp"


def test_explicit_base_call_resolves(tmp_path) -> None:
    (tmp_path / "base.h").write_bytes(b"class Base { public: int Shared(); };\n")
    (tmp_path / "base.cpp").write_bytes(b'#include "base.h"\nint Base::Shared() { return 1; }\n')
    (tmp_path / "derived.cpp").write_bytes(
        b"struct Derived : Base { int Run() { return Base::Shared(); } };\n")
    rec = _parse_with_index(tmp_path, "derived.cpp")
    assert next(c for c in _fn(rec, "Run").calls if c.name == "Shared").path == "base.cpp"


def test_external_base_does_not_resolve(tmp_path) -> None:
    # Base class is not in the repo (std::exception) → the inherited walk stops, no edge.
    (tmp_path / "e.cpp").write_bytes(
        b"struct MyErr : std::exception { int Run() { return what2(); } };\n")
    rec = _parse_with_index(tmp_path, "e.cpp")
    assert next(c for c in _fn(rec, "Run").calls if c.name == "what2").path is None


def test_out_of_class_definition_attaches_to_header_class(tmp_path) -> None:
    # A method defined in a .cpp attaches to its class node in the header (cross-file),
    # instead of orphaning to the .cpp file.
    (tmp_path / "judge.h").write_bytes(b"class Judge { public:\n int Decide();\n};\n")
    (tmp_path / "judge.cpp").write_bytes(
        b'#include "judge.h"\nint Judge::Decide() { return 1; }\n')
    parser = CppParser()
    files = list(tmp_path.rglob("*.h")) + list(tmp_path.rglob("*.cpp"))
    index = parser.build_index(tmp_path, files)
    from breezeai_cog.emit import class_id
    cpp = tmp_path / "judge.cpp"
    rec = parser.parse_file(ParseContext(path="judge.cpp", abs_path=cpp, source=cpp.read_bytes(),
                                         repo_root=tmp_path, resolution_index=index))
    decide = next(f for f in rec.functions if f.name == "Decide")
    assert decide.parentId == class_id("judge.h", "Judge")  # the header class node, not the file


def _parse_cross_file(tmp_path, target: str):
    parser = CppParser()
    files = list(tmp_path.rglob("*.h")) + list(tmp_path.rglob("*.cpp"))
    index = parser.build_index(tmp_path, files)
    p = tmp_path / target
    return parser.parse_file(ParseContext(path=target, abs_path=p, source=p.read_bytes(),
                                          repo_root=tmp_path, resolution_index=index))


def test_namespaced_definition_attaches_by_qualified_name(tmp_path) -> None:
    # Two classes share the simple name Foo in different namespaces/files. A .cpp definition must
    # attach to the class with the MATCHING fully-qualified name, never the other namesake.
    from breezeai_cog.emit import class_id
    (tmp_path / "a.h").write_bytes(b"namespace A { class Foo { public: int run(); }; }\n")
    (tmp_path / "b.h").write_bytes(b"namespace B { class Foo { public: int run(); }; }\n")
    (tmp_path / "a.cpp").write_bytes(b'#include "a.h"\nint A::Foo::run() { return 1; }\n')
    rec = _parse_cross_file(tmp_path, "a.cpp")
    run = next(f for f in rec.functions if f.name.endswith("run"))
    assert run.parentId == class_id("a.h", "Foo")  # A::Foo — never B::Foo in b.h


def test_definition_of_external_namesake_does_not_attach(tmp_path) -> None:
    # The repo has A::Widget; a .cpp defines a method on a DIFFERENT namespace's Widget, whose
    # class is not in the repo. The qualified name doesn't match A::Widget → no edge (honest-null).
    (tmp_path / "a.h").write_bytes(b"namespace A { class Widget { void paint(); }; }\n")
    (tmp_path / "ext.cpp").write_bytes(b"void Ext::Widget::paint() { }\n")
    rec = _parse_cross_file(tmp_path, "ext.cpp")
    paint = next(f for f in rec.functions if f.name.endswith("paint"))
    assert paint.parentId == "ext.cpp"  # orphaned to file, not attached to A::Widget


def test_enum_captured_with_member_statements(tmp_path) -> None:
    # Enum members become flat statements parented to the enum Class (queryable text).
    rec = _parse_src(tmp_path, b"enum Color { RED, GREEN, BLUE };\n", "c.h", capture=True)
    color = next((c for c in rec.classes if c.name == "Color"), None)
    assert color is not None and color.type == "enum" and color.metadata is None
    members = [s for s in rec.statements if s.parentId == color.id]
    assert [(m.name, m.text) for m in members] == [("RED", "RED"), ("GREEN", "GREEN"), ("BLUE", "BLUE")]
    assert all(m.nodeType == "enumerator" and m.semanticType is None for m in members)


def test_scoped_enum_is_enum(tmp_path) -> None:
    # enum class / enum struct are scoped enums — captured as type "enum", never "struct".
    rec = _parse_src(tmp_path, b"enum struct Mode : int { A = 1, B = 2 };\n", "m.h", capture=True)
    mode = next((c for c in rec.classes if c.name == "Mode"), None)
    assert mode is not None and mode.type == "enum" and mode.metadata is None
    members = [s for s in rec.statements if s.parentId == mode.id]
    assert [(m.name, m.text) for m in members] == [("A", "A = 1"), ("B", "B = 2")]


def test_union_has_its_own_type(tmp_path) -> None:
    # A union is a distinct kind — captured as type "union", not flattened to "struct".
    rec = _parse_src(tmp_path, b"union Value { int i; double d; };\n", "v.h")
    value = next((c for c in rec.classes if c.name == "Value"), None)
    assert value is not None and value.type == "union"


def test_nested_union_and_enum_captured(tmp_path) -> None:
    # A union/enum nested in a class parses as a field_declaration — both must still emit.
    src = b"class Outer {\n  union Chunk { int i; };\n  enum Kind { A, B };\n};\n"
    rec = _parse_src(tmp_path, src, "o.h", capture=True)
    by_name = {c.name: c.type for c in rec.classes}
    assert by_name.get("Chunk") == "union"
    assert by_name.get("Kind") == "enum"
    kind = next(c for c in rec.classes if c.name == "Kind")
    assert kind.metadata is None
    members = [s for s in rec.statements if s.parentId == kind.id]
    assert [(m.name, m.text) for m in members] == [("A", "A"), ("B", "B")]


def test_anonymous_and_forward_enum_not_fabricated(tmp_path) -> None:
    # Anonymous enum (no name) and a forward declaration (no body) emit nothing.
    rec = _parse_src(tmp_path, b"enum { X, Y };\nenum Fwd;\n", "a.h")
    assert not any(c.type == "enum" for c in rec.classes)


def _fn(rec: FileRecord, name: str):
    return next(f for f in rec.functions if f.name == name)


def test_template_class_captured(tmp_path) -> None:
    src = b"template<typename T>\nclass Box : public Base<T> {\npublic:\n  T* get(int i);\n};\n"
    p = tmp_path / "b.cpp"
    p.write_bytes(src)
    ctx = ParseContext(path="b.cpp", abs_path=p, source=src, repo_root=tmp_path)
    rec = CppParser().parse_file(ctx)
    box = next((c for c in rec.classes if c.name == "Box"), None)
    assert box is not None and box.extends == "Base"  # template base head
    assert "get" in {f.name for f in rec.functions}


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, capture=True)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def _parse_src(tmp_path, src: bytes, rel: str = "h.h", *, capture=False) -> FileRecord:
    p = tmp_path / rel
    p.write_bytes(src)
    return CppParser().parse_file(ParseContext(path=rel, abs_path=p, source=src,
                                               repo_root=tmp_path, capture_statements=capture))


def test_class_under_include_guard_is_captured(tmp_path) -> None:
    # Almost every header wraps its body in an `#ifndef GUARD` include guard; the parser must
    # descend through the preprocessor conditional to reach the class inside it.
    src = (
        b"#ifndef ACCESSOR_H\n#define ACCESSOR_H\n"
        b"namespace a { namespace b {\n"
        b"class Accessor { public: int fetch(int id); };\n"
        b"} }\n#endif\n"
    )
    rec = _parse_src(tmp_path, src)
    assert [c.name for c in rec.classes] == ["Accessor"]
    assert "fetch" in {f.name for f in rec.functions}


def test_forward_declaration_is_not_a_class(tmp_path) -> None:
    # `class Foo;` announces a name but defines nothing — it must not emit a hollow class node.
    src = b"class Fwd;\nclass Real { public: int go(); };\n"
    rec = _parse_src(tmp_path, src)
    names = {c.name for c in rec.classes}
    assert "Real" in names and "Fwd" not in names


# --- N2/N3: class member + enum members captured as flat statements -----------

def test_class_members_captured_as_statements(tmp_path) -> None:
    # C++ class member variables/constants are otherwise captured nowhere — emit each as a
    # flat statement parented to the class (their `text`, incl. any `= value`, is queryable).
    src = (b'class Codes {\n public:\n'
           b'  constexpr static const char* kFirst = "A1"; //!< first code\n'
           b'  static const int kMax = 9;                  //!< upper bound\n'
           b'  int plain;\n'
           b'};\n')
    rec = _parse_src(tmp_path, src, "h.h", capture=True)
    codes = next(c for c in rec.classes if c.name == "Codes")
    assert codes.metadata is None
    # A same-line trailing doc (``//!< …``) folds into the member's text — the high-value
    # constant-doc case (BREEZEAI-974): the human meaning rides on the member statement.
    members = [(s.name, s.text) for s in rec.statements if s.parentId == codes.id]
    assert members == [
        ("kFirst", 'constexpr static const char* kFirst = "A1"; //!< first code'),
        ("kMax", "static const int kMax = 9;                  //!< upper bound"),
        ("plain", "int plain;"),
    ]
    assert all(s.nodeType == "field_declaration" for s in rec.statements if s.parentId == codes.id)


def test_enum_values_captured_as_statements(tmp_path) -> None:
    # Enumerator members (with any `= value`) become flat statements parented to the enum.
    src = (b'enum Status {\n'
           b'  OK = 3,\n'
           b'  FAIL = 9,\n'
           b'  UNKNOWN\n'
           b'};\n')
    rec = _parse_src(tmp_path, src, "s.h", capture=True)
    st = next(c for c in rec.classes if c.name == "Status")
    assert st.metadata is None
    members = [(s.name, s.text) for s in rec.statements if s.parentId == st.id]
    assert members == [("OK", "OK = 3"), ("FAIL", "FAIL = 9"), ("UNKNOWN", "UNKNOWN")]


def test_class_members_gated_by_capture_flag(tmp_path) -> None:
    # Member/enumerator statements are gated by --capture-statements (absent without it).
    src = b'class C { public:\n constexpr static const char* k = "v";\n };\nenum E { A = 1 };\n'
    rec = _parse_src(tmp_path, src, "c.h", capture=False)
    assert rec.statements == []
    assert all(c.metadata is None for c in rec.classes)


def test_cpp_members_output_validates(tmp_path) -> None:
    src = (b'class C { public:\n constexpr static const char* k = "v"; //!< d\n };\n'
           b'enum E { A = 1 };\n')
    rec = _parse_src(tmp_path, src, "c.h", capture=True)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
