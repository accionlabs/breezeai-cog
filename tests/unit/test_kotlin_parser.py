from __future__ import annotations

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.kotlin.parser import KotlinParser
from breezeai_cog.schemas import FileRecord

SRC = b'''package com.acme.orders

import java.util.List
import com.acme.repo.OrderRepo

class OrderController {
    val repo: OrderRepo = OrderRepo()

    fun getOrder(id: Long): Order {
        return repo.findById(id)
    }
}

object Config
interface Repo {}
'''

REL = "src/main/kotlin/com/acme/orders/OrderController.kt"


def _parse(tmp_path) -> FileRecord:
    repo_dir = tmp_path / "src/main/kotlin/com/acme/repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "OrderRepo.kt").write_text("package com.acme.repo\nclass OrderRepo {}\n")
    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(SRC.decode())
    parser = KotlinParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.kt")))
    ctx = ParseContext(path=REL, abs_path=p, source=SRC, repo_root=tmp_path,
                       resolution_index=index, capture_statements=False)
    return parser.parse_file(ctx)


def test_imports_and_basic_structure(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert rec.language == "kotlin"
    assert "java.util.List" in rec.externalImports
    assert any(p.endswith("com/acme/repo/OrderRepo.kt") for p in rec.importFiles)
    assert {c.name for c in rec.classes} >= {"OrderController", "Config"}


def test_functions_and_primary_constructor(tmp_path) -> None:
    rec = _parse(tmp_path)
    fn = next(f for f in rec.functions if f.name == "getOrder")
    assert fn.type == "function"
    assert fn.params[0].name == "id"
    assert fn.params[0].type == "Long"
    assert any(c.name == "findById" for c in fn.calls)
