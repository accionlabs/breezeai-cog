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
