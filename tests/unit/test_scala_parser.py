"""Scala parser extraction tests + FQCN import resolution + schema validation."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from breezeai_cog.core.registry import capabilities, discover_builtin
from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.config.extractors import extract_config
from breezeai_cog.parsers.scala.parser import ScalaParser
from breezeai_cog.schemas import ConstructorParam, FileRecord

SRC = b"""package com.acme.orders

import java.util.List
import com.acme.repo.OrderRepo
import scala.concurrent.{Future, ExecutionContext}
import play.api.mvc._
import com.acme.util.{DateHelper => DH}

trait Greeter[T] {
  def greet(name: String): String
  def id: T
}

abstract class Base(val cfg: Config) {
  protected def hook(): Unit = {}
}

class OrderController @Inject() (repo: OrderRepo, cc: ControllerComponents)(implicit ec: ExecutionContext)
    extends Base(cfg) with Greeter[Long] with Serializable {

  private val max: Int = 5

  def list: Action[AnyContent] = Action.async { implicit req =>
    repo.findAll().map(os => Ok(Json.toJson(os)))
  }

  override def greet(name: String): String = s"hi $name"

  def compute[A <: Ordered[A]](xs: Seq[A]): Option[A] = xs.headOption
}

object OrderController {
  val DEFAULT = "none"
  def build(repo: OrderRepo): OrderController = new OrderController(repo, null)
  private def helper(x: Int) = x + 1
}

case class Order(id: Long, total: BigDecimal, note: Option[String])

case object Empty

enum Color(val hex: String):
  case Red extends Color("#f00")
  case Blue extends Color("#00f")
"""

REL = "src/main/scala/com/acme/orders/OrderController.scala"


def _parse(tmp_path: Path, src: bytes = SRC, *, capture: bool = False) -> tuple[FileRecord, ScalaParser]:
    repo_dir = tmp_path / "src/main/scala/com/acme/repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / "OrderRepo.scala").write_text("package com.acme.repo\ntrait OrderRepo\n")

    util_dir = tmp_path / "src/main/scala/com/acme/util"
    util_dir.mkdir(parents=True, exist_ok=True)
    (util_dir / "DateHelper.scala").write_text("package com.acme.util\nobject DateHelper\n")

    p = tmp_path / REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(src)

    parser = ScalaParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.scala")))
    ctx = ParseContext(
        path=REL,
        abs_path=p,
        source=src,
        repo_root=tmp_path,
        resolution_index=index,
        capture_statements=capture,
    )
    return parser.parse_file(ctx), parser


def test_capabilities_registered() -> None:
    discover_builtin()
    caps = capabilities()
    languages = caps["languages"]
    extensions = caps["extensions"]
    assert isinstance(languages, list)
    assert isinstance(extensions, list)
    assert "scala" in languages
    assert ".scala" in extensions
    assert ".sc" in extensions


def test_imports_and_fqcn_resolution(tmp_path: Path) -> None:
    rec, _ = _parse(tmp_path)
    assert rec.language == "scala"
    assert "java.util.List" in rec.externalImports
    assert "play.api.mvc.*" in rec.externalImports
    # FQCN resolved imports
    assert any(p.endswith("com/acme/repo/OrderRepo.scala") for p in rec.importFiles)
    assert any(p.endswith("com/acme/util/DateHelper.scala") for p in rec.importFiles)


def test_types_and_case_classes(tmp_path: Path) -> None:
    rec, _ = _parse(tmp_path)
    types_by_name = [(c.name, c.type) for c in rec.classes]
    # AC-2
    assert ("OrderController", "class") in types_by_name
    assert ("OrderController", "module") in types_by_name
    assert ("Greeter", "trait") in types_by_name
    assert ("Order", "record") in types_by_name  # case class -> record
    assert ("Empty", "module") in types_by_name  # case object -> module (D2)
    assert ("Color", "enum") in types_by_name  # enum -> enum

    # AC-3 Heritage
    ctrl = next(c for c in rec.classes if c.name == "OrderController" and c.type == "class")
    assert ctrl.extends == "Base"
    assert ctrl.implements == ["Greeter[Long]", "Serializable"]

    # Decorators
    assert any(d.name == "Inject" for d in ctrl.decorators)

    # AC-5 Constructor params (first class_parameters only; ec is absent)
    assert ctrl.constructorParams == [
        ConstructorParam(name="repo", type="OrderRepo"),
        ConstructorParam(name="cc", type="ControllerComponents"),
    ]

    # Visibility and abstractness
    greeter = next(c for c in rec.classes if c.name == "Greeter")
    base = next(c for c in rec.classes if c.name == "Base")
    assert ctrl.visibility == "public"
    assert ctrl.isAbstract is False
    assert greeter.isAbstract is True
    assert base.isAbstract is True


def test_object_members_captured(tmp_path: Path) -> None:
    # AC-4: Object members ARE captured
    rec, _ = _parse(tmp_path)
    obj_cls = next(c for c in rec.classes if c.name == "OrderController" and c.type == "module")

    build_fn = next((f for f in rec.functions if f.name == "build"), None)
    assert build_fn is not None
    assert build_fn.parentId == obj_cls.id
    assert build_fn.isStatic is True
    assert build_fn.type == "method"

    # Static functions exist
    static_fns = [f for f in rec.functions if f.isStatic]
    assert len(static_fns) >= 2  # build, helper

    # Nested object in class
    nested_src = b"""package com.test
class Outer {
  object Inner {
    def inside(): Unit = {}
  }
}
"""
    rec_nested, _ = _parse(tmp_path, nested_src)
    outer = next(c for c in rec_nested.classes if c.name == "Outer")
    inner = next(c for c in rec_nested.classes if c.name == "Inner")
    assert inner.parentId == outer.id
    assert inner.type == "module"
    inside_fn = next(f for f in rec_nested.functions if f.name == "inside")
    assert inside_fn.parentId == inner.id
    assert inside_fn.isStatic is True


def test_abstract_trait_members_and_inferred_return_type(tmp_path: Path) -> None:
    rec, _ = _parse(tmp_path)
    greet = next(f for f in rec.functions if f.name == "greet" and f.returnType == "String" and f.calls == [])
    assert greet is not None  # abstract function_declaration

    helper = next(f for f in rec.functions if f.name == "helper")
    assert helper.returnType is None  # honest-null for inferred return type


def test_statement_gating_and_capture(tmp_path: Path) -> None:
    # Gated: False -> no statements
    rec_off, _ = _parse(tmp_path, capture=False)
    assert rec_off.statements == []

    # Gated: True -> statements captured
    rec_on, _ = _parse(tmp_path, capture=True)
    assert len(rec_on.statements) > 0


def test_scala3_indentation_syntax(tmp_path: Path) -> None:
    src3 = b"""package com.acme.orders

class Svc(repo: Repo):
  def find(id: Long): Option[User] =
    val q = repo.get(id)
    q

object Svc:
  def apply(): Svc = new Svc(null)
"""
    rec, _ = _parse(tmp_path, src3, capture=True)
    by_name = {c.name: c for c in rec.classes}
    assert "Svc" in by_name
    find_fn = next(f for f in rec.functions if f.name == "find")
    assert find_fn.returnType == "Option[User]"
    # Statements inside indented_block
    assert len(rec.statements) > 0


def test_comment_capture(tmp_path: Path) -> None:
    src_comments = b"""package com.acme

// Single line comment
class Foo {
  /* Block comment */
  def bar(): Unit = {
    // Inside method
  }
}
"""
    rec, _ = _parse(tmp_path, src_comments, capture=True)
    comments = [s for s in rec.statements if s.semanticType == "comment"]
    assert len(comments) >= 3


def test_scala3_enum_members(tmp_path: Path) -> None:
    src_enum = b"""package com.acme

enum Status:
  case Active
  case Inactive
"""
    rec, _ = _parse(tmp_path, src_enum, capture=True)
    enum_cls = next(c for c in rec.classes if c.name == "Status")
    enum_members = [s for s in rec.statements if s.parentId == enum_cls.id and s.name in ("Active", "Inactive")]
    assert len(enum_members) == 2


def test_multi_type_file_fqcn(tmp_path: Path) -> None:
    src_multi = b"""package com.acme.models

class First
class Second
trait Third
object Fourth
"""
    f = tmp_path / "Multi.scala"
    f.write_bytes(src_multi)
    parser = ScalaParser()
    index = parser.build_index(tmp_path, [f])
    assert index.fqcn.get("com.acme.models.First") is not None
    assert index.fqcn.get("com.acme.models.Second") is not None
    assert index.fqcn.get("com.acme.models.Third") is not None
    assert index.fqcn.get("com.acme.models.Fourth") is not None


def test_build_sbt_extraction() -> None:
    sbt_text = """
name := "my-project"
version := "0.1.0"
scalaVersion := "2.13.12"

libraryDependencies ++= Seq(
  "com.typesafe.play" %% "play" % "2.8.19",
  "org.scalatestplus.play" %% "scalatestplus-play" % "5.1.0" % Test,
  "com.typesafe.akka" %% "akka-actor" % "2.6.20"
)
"""
    meta = extract_config("build.sbt", sbt_text)
    assert meta["kind"] == "sbt"
    assert meta["category"] == "sbt"
    assert meta["buildTool"] == "sbt"
    assert meta["packageManager"] == "sbt"
    assert meta["dependencyCount"] == 3


def test_schema_validity(tmp_path: Path) -> None:
    rec, _ = _parse(tmp_path, capture=True)
    schema = FileRecord.model_json_schema(by_alias=True)
    validator = Draft202012Validator(schema)
    line = json.loads(to_line(rec))
    validator.validate(line)


# --------------------------------------------------------------------------- regressions
# Each of the four defects below was found by dogfooding, NOT by the suite above:
# the original fixtures only exercised class and object bodies, so the nested-def and
# file-scope paths were never executed. Keep these.

NESTED_DEF_SRC = b"""package com.acme

object Outer {
  def outer(n: Int): Int = {
    val before = n * 2

    def inner(x: Int): Int = {
      val secret = 42
      helper(x + secret)
    }

    def sibling(): String = {
      val tag = "s"
      tag
    }

    inner(before) + sibling().length
  }
}
"""


def test_nested_def_is_captured_as_its_own_function(tmp_path: Path) -> None:
    """Defect C: a nested `def` was dropped entirely."""
    rec, _ = _parse(tmp_path, NESTED_DEF_SRC, capture=True)
    names = {f.name for f in rec.functions}
    assert {"outer", "inner", "sibling"} <= names, f"nested defs missing: {names}"


def test_nested_def_statements_are_not_misattributed(tmp_path: Path) -> None:
    """Defect C, the damaging half: `secret` belongs to `inner`, never to `outer`.

    This is the wrong-data case — worse than missing data, because a consumer cannot
    tell it is wrong.
    """
    rec, _ = _parse(tmp_path, NESTED_DEF_SRC, capture=True)
    by_name = {f.name: f.id for f in rec.functions}
    owner = {s.name: s.parentId for s in rec.statements if s.name}

    assert owner["secret"] == by_name["inner"]
    assert owner["tag"] == by_name["sibling"]
    assert owner["before"] == by_name["outer"]
    # and nothing from a nested def leaked upward
    outer_stmts = {s.name for s in rec.statements if s.parentId == by_name["outer"] and s.name}
    assert "secret" not in outer_stmts
    assert "tag" not in outer_stmts


def test_nested_def_calls_are_not_double_counted(tmp_path: Path) -> None:
    """A call inside a nested def belongs to that def, not to the enclosing one."""
    rec, _ = _parse(tmp_path, NESTED_DEF_SRC, capture=True)
    calls = {f.name: {c.name for c in f.calls} for f in rec.functions}
    assert "helper" in calls["inner"]
    assert "helper" not in calls["outer"], "enclosing fn folded the nested def's call"
