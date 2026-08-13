"""Java parser extraction tests + FQCN import resolution + schema validation."""

from __future__ import annotations

import json

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java.parser import JavaParser
from breezeai_cog.schemas import ConstructorParam, FileRecord

SRC = b'''package com.acme.orders;

import java.util.List;
import com.acme.repo.OrderRepo;

@RestController
@RequestMapping("/orders")
public class OrderController extends Base implements IController {
    private final OrderRepo repo;
    public static final int MAX = 5;

    public OrderController(OrderRepo repo) { this.repo = repo; }

    @GetMapping("/{id}")
    public Order getOrder(@PathVariable Long id) {
        return repo.findById(id);
    }
}

interface IController {}
enum Status { OPEN, CLOSED }
'''

REL = "src/main/java/com/acme/orders/OrderController.java"


def _parse(tmp_path, *, capture=False) -> FileRecord:
    repo_dir = tmp_path / "src/main/java/com/acme/repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "OrderRepo.java").write_text("package com.acme.repo;\npublic interface OrderRepo {}\n")
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SRC.decode())
    parser = JavaParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.java")))
    ctx = ParseContext(path=REL, abs_path=p, source=SRC, repo_root=tmp_path,
                       resolution_index=index, capture_statements=capture)
    return parser.parse_file(ctx)


def test_imports_and_fqcn_resolution(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert rec.language == "java"
    assert "java.util.List" in rec.externalImports
    assert any(p.endswith("com/acme/repo/OrderRepo.java") for p in rec.importFiles)  # FQCN resolved


def test_types(tmp_path) -> None:
    rec = _parse(tmp_path)
    by_name = {c.name: c for c in rec.classes}
    assert by_name["OrderController"].type == "class"
    assert by_name["IController"].type == "interface"
    assert by_name["Status"].type == "enum"
    ctrl = by_name["OrderController"]
    assert ctrl.extends == "Base" and ctrl.implements == ["IController"]
    assert {d.name for d in ctrl.decorators} == {"RestController", "RequestMapping"}
    assert ctrl.constructorParams == [ConstructorParam(name="repo", type="OrderRepo")]
    # B1.2 required fields
    assert ctrl.visibility == "public" and ctrl.isAbstract is False
    assert by_name["IController"].isAbstract is True  # interfaces are abstract


def test_methods(tmp_path) -> None:
    rec = _parse(tmp_path)
    get = next(f for f in rec.functions if f.name == "getOrder")
    assert get.type == "method" and get.visibility == "public" and get.returnType == "Order"
    assert [d.name for d in get.decorators] == ["GetMapping"]
    assert get.params[0].name == "id" and get.params[0].type == "Long"
    assert [d.name for d in get.params[0].decorators] == ["PathVariable"]
    assert "findById" in [c.name for c in get.calls]
    ctrl = next(c for c in rec.classes if c.name == "OrderController")
    assert get.parentId == ctrl.id  # HAS_METHOD wiring
    assert any(f.type == "constructor" for f in rec.functions)


def test_statements_and_detection(tmp_path) -> None:
    assert _parse(tmp_path, capture=False).statements == []
    rec = _parse(tmp_path, capture=True)
    db = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert db and db[0].dataAccessHint  # repo.findById(...) detected as a DB call


def test_output_validates(tmp_path) -> None:
    rec = _parse(tmp_path, capture=True)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors


def test_same_package_call_resolves_without_import(tmp_path) -> None:
    # A same-package class needs no import, so `Helper.go()` must still resolve cross-file
    # via the FQCN index (seeded into the call-resolution bindings).
    (tmp_path / "Helper.java").write_text(
        "package a.b;\npublic class Helper { public static String go() { return \"x\"; } }\n"
    )
    main = tmp_path / "Main.java"
    main.write_text("package a.b;\npublic class Main { void run() { Helper.go(); } }\n")
    parser = JavaParser()
    idx = parser.build_index(tmp_path, list(tmp_path.rglob("*.java")))
    rec = parser.parse_file(ParseContext(path="Main.java", abs_path=main,
                                         source=main.read_bytes(), repo_root=tmp_path,
                                         resolution_index=idx, capture_statements=True))
    go = next(c for f in rec.functions for c in f.calls if c.name == "go")
    assert go.path == "Helper.java"


def test_deep_string_concat_does_not_recurse(tmp_path) -> None:
    # Regression: a statement with a very deep `+` chain (e.g. a generated HTML/JS builder
    # with hundreds of concats) is a left-nested binary_expression tree that folding recurses
    # through → RecursionError. render_concat must bail past its depth cap.
    import sys

    body = " + ".join(['"a"'] * 800)  # 800-deep concat — over the 100 cap, near the 1000 limit
    # must be a CALL ARGUMENT — that's what triggers endpoint rendering (render_concat)
    src = ("class C { void m() { sink(" + body + "); } }").encode()
    p = tmp_path / "C.java"
    p.write_text(src.decode())
    ctx = ParseContext(path="C.java", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)  # ensure the default limit is in force for the assertion
    try:
        rec = JavaParser().parse_file(ctx)  # must NOT raise RecursionError
    finally:
        sys.setrecursionlimit(old)
    assert rec.functions  # the method is still captured; the concat just yields no endpoint


def test_short_concat_still_renders_endpoint() -> None:
    # The depth cap must not regress normal URL/path concatenation rendering.
    from breezeai_cog.parsers.java.statements import _render_url
    from breezeai_cog.parsers.treesitter import parse_source

    src = b'class C { void m() { String u = "http://api/" + id + "/x"; } }'
    root = parse_source("java", src, 0).root_node
    concat = next(n for n in _walk(root) if n.type == "binary_expression")
    assert _render_url(concat, src) == "http://api/{id}/x"


def _walk(node):
    yield node
    for c in node.named_children:
        yield from _walk(c)


def test_deep_concat_tracked_and_summarized(tmp_path) -> None:
    # Deep concats are collected per file and rendered as ONE human-readable summary line.
    import breezeai_cog.parsers.statements_common as sc

    body = " + ".join(['"a"'] * 400)
    src = ("class C { void m() { sink(" + body + "); } }").encode()  # call arg triggers render
    p = tmp_path / "C.java"
    p.write_text(src.decode())
    ctx = ParseContext(path="C.java", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)

    sc.begin_concat_tracking()
    JavaParser().parse_file(ctx)
    summary = sc.summarize_skipped_concats("app/C.java")

    assert summary is not None
    assert summary.startswith("app/C.java:")
    assert "1 deeply-nested string concatenation" in summary  # count + phrasing
    assert "still\n captured" not in summary  # sanity: it's one readable line
    assert "Line: 1" in summary
    # collector is cleared after summarizing
    assert sc.summarize_skipped_concats("app/C.java") is None


def test_inline_lambda_body_captured(tmp_path) -> None:
    # Regression (#1): statements & calls inside a lambda are attributed to the
    # nearest named enclosing method, not dropped.
    src = (
        "class C {\n"
        "  void m(java.util.List<Order> orders) {\n"
        "    orders.forEach(o -> {\n"
        "      repo.save(o);\n"
        "      logger.info(o);\n"
        "    });\n"
        "  }\n"
        "}\n"
    ).encode()
    p = tmp_path / "C.java"
    p.write_text(src.decode())
    ctx = ParseContext(path="C.java", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = JavaParser().parse_file(ctx)
    m = next(f for f in rec.functions if f.name == "m")
    assert {"save", "info"} <= {c.name for c in m.calls}
    db = [s for s in rec.statements if s.semanticType == "db_method_call"]
    assert any("repo.save" in s.text for s in db)


def test_nested_member_classes_extracted(tmp_path) -> None:
    # Regression (code-capture-gap): member (inner) classes / interfaces / enums,
    # and their methods, must each be extracted as their own node parented to the
    # enclosing class — not dropped. Nesting recurses to arbitrary depth.
    src = (
        "package com.x;\n"
        "public class Outer {\n"
        "  void run() { java.util.List.of().forEach(o -> handle(o)); }\n"
        "  static class Inner {\n"
        "    void innerMethod() { innerCall(); }\n"
        "    class DeepInner { void deep() { deepCall(); } }\n"
        "  }\n"
        "  interface Callback { void onDone(); }\n"
        "  enum Status { OK, FAIL }\n"
        "}\n"
    ).encode()
    p = tmp_path / "Outer.java"
    p.write_text(src.decode())
    ctx = ParseContext(path="Outer.java", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = JavaParser().parse_file(ctx)
    by_cls = {c.name: c for c in rec.classes}
    assert {"Outer", "Inner", "DeepInner", "Callback", "Status"} <= set(by_cls)
    assert by_cls["Callback"].type == "interface" and by_cls["Status"].type == "enum"
    assert by_cls["Inner"].parentId == by_cls["Outer"].id
    assert by_cls["DeepInner"].parentId == by_cls["Inner"].id
    fn_names = {f.name for f in rec.functions}
    assert {"innerMethod", "deep", "onDone"} <= fn_names
    # the lambda in run() still folds into run (anonymous scope)
    assert "handle" in {c.name for f in rec.functions if f.name == "run" for c in f.calls}


def test_control_statement_not_mislabeled(tmp_path) -> None:
    # #4/smear: a db call nested in an if/for body must not tag the enclosing control statements.
    src = ("class C { void m(java.util.List<Order> o){ if(o.size()>0){ for(Order x: o){ repo.save(x); } } } }").encode()
    p = tmp_path / "C.java"
    p.write_text(src.decode())
    ctx = ParseContext(path="C.java", abs_path=p, source=src, repo_root=tmp_path, capture_statements=True)
    rec = JavaParser().parse_file(ctx)
    control = [s for s in rec.statements if s.nodeType in ("if_statement", "enhanced_for_statement", "for_statement")]
    assert control and all(s.semanticType is None for s in control)
    assert any(s.semanticType == "db_method_call" and s.method == "save" for s in rec.statements)


def test_endpoint_concatenation(tmp_path) -> None:
    # #3: Java string concatenation "/users/" + id -> /users/{id}.
    src = 'class C { void m(String id){ httpClient.get("/users/" + id); } }'.encode()
    p = tmp_path / "C.java"
    p.write_text(src.decode())
    ctx = ParseContext(path="C.java", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = JavaParser().parse_file(ctx)
    assert any(s.semanticType == "api_call" and s.endpoint == "/users/{id}" for s in rec.statements)


# --- N1: enum members captured as flat statements (queryable text) ------------

def _enum_members(tmp_path, src: str):
    p = tmp_path / "E.java"
    p.write_text(src)
    ctx = ParseContext(path="E.java", abs_path=p, source=src.encode(), repo_root=tmp_path,
                       capture_statements=True)
    rec = JavaParser().parse_file(ctx)
    enum = next(c for c in rec.classes if c.type == "enum")
    members = [s for s in rec.statements if s.parentId == enum.id]
    return rec, enum, members


def test_enum_members_captured_as_statements(tmp_path) -> None:
    # NAME("value") members become flat statements parented to the enum Class; the value
    # rides inside the statement text (no more metadata.constants channel).
    src = ('enum Priority {\n'
           '  /** high urgency */\n'
           '  HIGH("3"),\n'
           '  /** low urgency */\n'
           '  LOW("1");\n}\n')
    _, enum, members = _enum_members(tmp_path, src)
    assert enum.metadata is None
    assert [(m.name, m.text) for m in members] == [("HIGH", 'HIGH("3")'), ("LOW", 'LOW("1")')]
    assert all(m.nodeType == "enum_constant" and m.semanticType is None for m in members)


def test_enum_bare_and_valued_members(tmp_path) -> None:
    # Bare (no-arg) and valued constants both captured; each member's own text is its span.
    src = 'enum Color {\n  RED("f00"),\n  GREEN("0f0"),\n  BLUE;\n}\n'
    _, _, members = _enum_members(tmp_path, src)
    assert [(m.name, m.text) for m in members] == [
        ("RED", 'RED("f00")'), ("GREEN", 'GREEN("0f0")'), ("BLUE", "BLUE"),
    ]


def test_enum_members_gated_by_capture_flag(tmp_path) -> None:
    # Enum members are statements now → gated by --capture-statements (absent without it).
    src = 'enum S { A("1"), B("2"); }'
    p = tmp_path / "E.java"
    p.write_text(src)
    ctx = ParseContext(path="E.java", abs_path=p, source=src.encode(), repo_root=tmp_path,
                       capture_statements=False)
    rec = JavaParser().parse_file(ctx)
    assert rec.statements == []
    assert all(c.metadata is None for c in rec.classes)


def test_plain_class_metadata_stays_none(tmp_path) -> None:
    # A `NAME = "value"` field lands as a statement; the class carries no constants metadata.
    src = 'class A { private static final String NAME = "value"; }'
    p = tmp_path / "A.java"
    p.write_text(src)
    ctx = ParseContext(path="A.java", abs_path=p, source=src.encode(), repo_root=tmp_path,
                       capture_statements=True)
    rec = JavaParser().parse_file(ctx)
    assert all(c.metadata is None for c in rec.classes)
    assert any((s.text or "").find("NAME") >= 0 for s in rec.statements)


def test_enum_members_output_validates(tmp_path) -> None:
    src = 'enum S {\n  A("1"),\n  B("2");\n}\n'
    p = tmp_path / "E.java"
    p.write_text(src)
    ctx = ParseContext(path="E.java", abs_path=p, source=src.encode(), repo_root=tmp_path,
                       capture_statements=True)
    rec = JavaParser().parse_file(ctx)
    errors = list(Draft202012Validator(FileRecord.model_json_schema(by_alias=True))
                  .iter_errors(json.loads(to_line(rec))))
    assert not errors, errors
