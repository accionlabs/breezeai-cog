"""Calls no method body holds, and the constructor an anonymous subclass runs (java parser).

* ``new X(…) { … }`` calls X's constructor from the method that creates it, when X is certainly
  a class of another repository file (constructor arguments, or declared as a class).
* A member type a class inherits (``Handler`` declared in a base class) is found by its simple
  name: it gives the anonymous class its ``extends`` and the creation its file.
* ``import a.Outer.Inner;`` resolves to ``Outer``'s file.
* A statement outside any method — a field initializer, a ``static { … }`` or ``{ … }`` block
  statement, an enum constant — carries its calls (``Statement.calls``).
"""

from __future__ import annotations

from pathlib import Path

from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java.parser import JavaParser
from breezeai_cog.schemas import FileRecord

_ROOT = "src/main/java"
_H = f"{_ROOT}/h"

FILES = {
    # the base declares nested types its subclasses use without an import
    "h/ApiHandler.java": """package h;
public abstract class ApiHandler {
  public static class ApiConfig {
    public ApiConfig() {}
  }
  public static abstract class ResponseHandler {
    public ResponseHandler() {}
    public abstract void handle(Object o);
  }
  public interface Listener { void on(Object o); }
}
""",
    "h/NoticeApiHandler.java": "package h;\npublic class NoticeApiHandler extends ApiHandler { }\n",
    "h/ResponseData.java": """package h;
public class ResponseData {
  protected ResponseData(int code, Object body) {}
}
""",
    "h/Registry.java": "package h;\npublic class Registry { public static void add(Object o) {} }\n",
    "h/WriteHandler.java": "package h;\npublic class WriteHandler { public WriteHandler() {} }\n",
    "h/Util.java": "package h;\npublic class Util { public static String norm(String s) { return s; } }\n",
    "h/DataWriteApiHandler.java": """package h;
public class DataWriteApiHandler extends NoticeApiHandler {
  private final ResponseHandler errorAlert = new ResponseHandler() {
    public void handle(Object o) {}
  };
  protected ResponseData create(int code, Object body) {
    return new ResponseData(code, body) {
      public String toString() { return "x"; }
    };
  }
  void listen() {
    Object l = new Listener() { public void on(Object o) {} };
    Object r = new Runnable() { public void run() {} };
  }
  void respond() {
    Object h = new ResponseHandler() { public void handle(Object o) {} };
  }
}
""",
    # a nested class imported by its full name, in a field initializer
    "h/ApiHandlerBuilder.java": """package h;
import h.ApiHandler.ApiConfig;
public class ApiHandlerBuilder {
  private ApiConfig config = new ApiConfig();
  private String name = Util.norm("b");
  public ApiHandlerBuilder() {}
}
""",
    "h/ServiceServer.java": """package h;
import java.util.HashMap;
public class ServiceServer {
  static HashMap<String, Object> bus;
  static {
    bus = new HashMap<>();
    bus.put("a/Write", new WriteHandler());
    if (bus.isEmpty()) {
      Registry.add(new WriteHandler());
    }
  }
  public void start() {}
}
""",
    "h/Client.java": """package h;
public interface Client {
  enum KIND {
    NORMAL("1", new WriteHandler()),
    PLAIN("2", null);
    KIND(String no, Object c) {}
  }
}
""",
}


def _parse(tmp_path: Path, target: str) -> FileRecord:
    for rel, src in FILES.items():
        p = tmp_path / _ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    parser = JavaParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.java")))
    rel = f"{_ROOT}/{target}"
    path = tmp_path / rel
    ctx = ParseContext(path=rel, abs_path=path, source=path.read_bytes(), repo_root=tmp_path,
                       resolution_index=index, capture_statements=True)
    return parser.parse_file(ctx)


def _fn_calls(rec: FileRecord) -> dict[str, dict[str, str | None]]:
    return {f.name: {c.name: c.path for c in f.calls} for f in rec.functions}


def _stmt_calls(rec: FileRecord) -> dict[int, dict[str, str | None]]:
    return {s.startLine: {c.name: c.path for c in s.calls} for s in rec.statements if s.calls}


def test_an_anonymous_subclass_calls_its_class_constructor(tmp_path: Path) -> None:
    calls = _fn_calls(_parse(tmp_path, "h/DataWriteApiHandler.java"))
    # constructor arguments: certainly a class
    assert calls["create"] == {"ResponseData": f"{_H}/ResponseData.java"}
    # no arguments: an inherited member type declared as a class, in the base's file
    assert calls["respond"] == {"ResponseHandler": f"{_H}/ApiHandler.java"}


def test_an_anonymous_interface_or_library_type_calls_nothing(tmp_path: Path) -> None:
    calls = _fn_calls(_parse(tmp_path, "h/DataWriteApiHandler.java"))
    assert calls["listen"] == {}  # an in-repo interface and a JDK interface: no constructor


def test_an_inherited_member_type_gives_the_anonymous_class_its_extends(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/DataWriteApiHandler.java")
    anon = {c.name: c for c in rec.classes if c.isAnonymous}
    assert anon["errorAlert"].extends == "ResponseHandler"
    assert anon["DataWriteApiHandler$1"].extends == "ResponseData"
    assert anon["DataWriteApiHandler$2"].implements == ["Listener"]  # inherited interface


def test_a_nested_class_import_resolves_to_its_outer_file(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/ApiHandlerBuilder.java")
    assert f"{_H}/ApiHandler.java" in rec.importFiles
    assert "h.ApiHandler.ApiConfig" not in rec.externalImports


def test_a_field_initializer_carries_its_calls(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/ApiHandlerBuilder.java")
    stmts = _stmt_calls(rec)
    assert stmts[4] == {"ApiConfig": f"{_H}/ApiHandler.java"}
    assert stmts[5] == {"norm": f"{_H}/Util.java"}
    assert _fn_calls(rec)["ApiHandlerBuilder"] == {}  # the constructor's own body: unchanged


def test_a_static_block_statement_carries_its_own_calls(tmp_path: Path) -> None:
    stmts = _stmt_calls(_parse(tmp_path, "h/ServiceServer.java"))
    assert stmts[7] == {"put": None, "WriteHandler": f"{_H}/WriteHandler.java"}
    # the `if` keeps its condition's call; the statement in its body keeps its own
    assert stmts[8] == {"isEmpty": None}
    assert stmts[9] == {"add": f"{_H}/Registry.java", "WriteHandler": f"{_H}/WriteHandler.java"}
    assert 6 not in stmts  # `new HashMap<>()`: a library class, no call


def test_an_enum_constant_carries_its_argument_calls(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/Client.java")
    stmts = _stmt_calls(rec)
    assert stmts[4] == {"WriteHandler": f"{_H}/WriteHandler.java"}
    assert 5 not in stmts  # no call: no `calls`


def test_a_statement_inside_a_method_carries_no_calls(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/DataWriteApiHandler.java")
    method_ids = {f.id for f in rec.functions}
    assert all(s.calls is None for s in rec.statements if s.parentId in method_ids)
