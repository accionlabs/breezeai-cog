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


def test_catch_finally_clauses_emitted(tmp_path) -> None:
    src = b"fun m() { try { save() } catch (e: Exception) { log(e) } finally { cleanup() } }\n"
    p = tmp_path / "e.kt"
    p.write_text(src.decode())
    ctx = ParseContext(path="e.kt", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = KotlinParser().parse_file(ctx)
    node_types = {s.nodeType for s in rec.statements}
    assert {"try_expression", "catch_block", "finally_block"} <= node_types


def test_imports_and_basic_structure(tmp_path) -> None:
    rec = _parse(tmp_path)
    assert rec.language == "kotlin"
    assert "java.util.List" in rec.externalImports
    assert any(p.endswith("com/acme/repo/OrderRepo.kt") for p in rec.importFiles)
    assert {c.name for c in rec.classes} >= {"OrderController", "Config"}


def test_functions_and_primary_constructor(tmp_path) -> None:
    rec = _parse(tmp_path)
    fn = next(f for f in rec.functions if f.name == "getOrder")
    # Class members are methods (not plain functions), and not static.
    assert fn.type == "method"
    assert fn.isStatic is False
    assert fn.params[0].name == "id"
    assert fn.params[0].type == "Long"
    assert any(c.name == "findById" for c in fn.calls)


_LOCAL_FN_SRC = b'''\
package com.acme

fun processOrder(id: Int): Int {
    fun applyTax(amount: Int) = amount * 2
    return applyTax(id)
}
'''


def test_local_function_inside_body(tmp_path) -> None:
    """Local functions declared inside a function body are captured as separate Function records."""
    p = tmp_path / "LocalFn.kt"
    p.write_text(_LOCAL_FN_SRC.decode())
    ctx = ParseContext(
        path="LocalFn.kt",
        abs_path=p,
        source=_LOCAL_FN_SRC,
        repo_root=tmp_path,
        capture_statements=True,
    )
    rec = KotlinParser().parse_file(ctx)
    fn_names = {f.name for f in rec.functions}
    assert "processOrder" in fn_names
    assert "applyTax" in fn_names, f"Local function applyTax not captured; got: {fn_names}"


def test_enum_entries_captured_as_statements(tmp_path) -> None:
    # Enum entries become flat statements parented to the enum Class (queryable text).
    src = b"enum class Dir { NORTH, SOUTH }\n"
    p = tmp_path / "d.kt"
    p.write_bytes(src)
    ctx = ParseContext(path="d.kt", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = KotlinParser().parse_file(ctx)
    d = next(c for c in rec.classes if c.type == "enum")
    members = [(s.name, s.text, s.nodeType) for s in rec.statements if s.parentId == d.id]
    assert members == [("NORTH", "NORTH", "enum_entry"), ("SOUTH", "SOUTH", "enum_entry")]
    ctx2 = ParseContext(path="d.kt", abs_path=p, source=src, repo_root=tmp_path,
                        capture_statements=False)
    assert KotlinParser().parse_file(ctx2).statements == []


# BREEZEAI-839: companion members, method typing, suspend/receiver metadata,
# implements type args, and sealed marking.
_GAPS_SRC = b'''\
package com.acme

interface Repository<T> {
    fun findById(id: String): T?
}

abstract class ServiceBase(val audit: Audit) {
    abstract fun flush()
}

sealed class Result

object Registry {
    fun register(x: Int) {}
}

class OrderService(audit: Audit) : ServiceBase(audit), Repository<Order> {
    companion object {
        const val MAX = 100
        fun <R> mapAll(src: List<Order>, f: (Order) -> R): List<R> = src.map(f)
    }
    override fun findById(id: String): Order? = null
    override fun flush() {}
    suspend fun loadAsync(id: String): Order? = null
}

fun String.slugify(): String = this.lowercase()
'''


def _parse_gaps(tmp_path) -> FileRecord:
    p = tmp_path / "Gaps.kt"
    p.write_bytes(_GAPS_SRC)
    ctx = ParseContext(path="Gaps.kt", abs_path=p, source=_GAPS_SRC,
                       repo_root=tmp_path, capture_statements=True)
    return KotlinParser().parse_file(ctx)


def test_companion_object_function_captured_as_static_method(tmp_path) -> None:
    rec = _parse_gaps(tmp_path)
    mapall = next((f for f in rec.functions if f.name == "mapAll"), None)
    assert mapall is not None, "companion object function mapAll not captured"
    assert mapall.type == "method"
    assert mapall.isStatic is True
    svc = next(c for c in rec.classes if c.name == "OrderService")
    assert mapall.parentId == svc.id, "companion member must attach to enclosing class"
    # const val is captured as a statement attached to the enclosing class.
    max_stmt = next((s for s in rec.statements if s.name == "MAX"), None)
    assert max_stmt is not None and max_stmt.parentId == svc.id


def test_class_members_typed_method_with_is_static(tmp_path) -> None:
    rec = _parse_gaps(tmp_path)
    instance = next(f for f in rec.functions if f.name == "flush" and f.type == "method")
    assert instance.isStatic is False
    # object (singleton) members are static methods.
    register = next(f for f in rec.functions if f.name == "register")
    assert register.type == "method" and register.isStatic is True
    # top-level extension stays a function.
    slug = next(f for f in rec.functions if f.name == "slugify")
    assert slug.type == "function"


def test_suspend_and_receiver_fields(tmp_path) -> None:
    rec = _parse_gaps(tmp_path)
    # suspend → first-class isAsync (language-neutral async boundary).
    load = next(f for f in rec.functions if f.name == "loadAsync")
    assert load.isAsync is True
    assert load.receiverType is None
    # extension receiver → first-class receiverType (base type only).
    slug = next(f for f in rec.functions if f.name == "slugify")
    assert slug.receiverType == "String"
    assert slug.isAsync is None
    # A plain instance method carries neither.
    plain = next(f for f in rec.functions if f.name == "findById")
    assert plain.isAsync is None and plain.receiverType is None


def test_implements_preserves_type_arguments(tmp_path) -> None:
    rec = _parse_gaps(tmp_path)
    svc = next(c for c in rec.classes if c.name == "OrderService")
    assert svc.implements == ["Repository<Order>"]
    assert svc.extends == "ServiceBase"


def test_sealed_class_field(tmp_path) -> None:
    rec = _parse_gaps(tmp_path)
    # sealed → first-class isSealed (sibling of isAbstract).
    result = next(c for c in rec.classes if c.name == "Result")
    assert result.isSealed is True
    # non-sealed classes carry no isSealed marker.
    svc = next(c for c in rec.classes if c.name == "OrderService")
    assert svc.isSealed is None


def test_property_annotations_captured_on_statements(tmp_path) -> None:
    # A decorated property declaration is flattened into a Statement; its annotations
    # must be structured (not left only in raw text).
    src = b"class Svc {\n  @Autowired\n  lateinit var repo: UserRepository\n}\n"
    p = tmp_path / "Svc.kt"
    p.write_bytes(src)
    ctx = ParseContext(path="Svc.kt", abs_path=p, source=src, repo_root=tmp_path,
                       capture_statements=True)
    rec = KotlinParser().parse_file(ctx)
    prop = next(s for s in rec.statements if s.name == "repo")
    assert [(d.name, d.args) for d in prop.decorators] == [("Autowired", [])]
