"""Calls no method body holds, and the constructor an anonymous subclass runs (java parser).

* ``new X(…) { … }`` calls X's constructor from the method that creates it, when X is certainly
  a class of another repository file (constructor arguments, or declared as a class).
* A member type a class inherits (``Handler`` declared in a base class) is found by its simple
  name: it gives the anonymous class its ``extends`` and the creation its file.
* ``import a.Outer.Inner;`` resolves to ``Outer``'s file.
* The code outside any method — field initializers, ``static { … }`` and ``{ … }`` blocks, enum
  constants' arguments — has its calls on its class (``Class.calls``), statement capture or not.
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
    "h/Defaults.java": """package h;
import java.util.HashMap;
public class Defaults {
  void make() {
    Object m = new HashMap<String, Object>() {{
      put("w", new WriteHandler());
    }};
  }
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


def _parse(tmp_path: Path, target: str, *, capture: bool = True) -> FileRecord:
    for rel, src in FILES.items():
        p = tmp_path / _ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    parser = JavaParser()
    index = parser.build_index(tmp_path, list(tmp_path.rglob("*.java")))
    rel = f"{_ROOT}/{target}"
    path = tmp_path / rel
    ctx = ParseContext(path=rel, abs_path=path, source=path.read_bytes(), repo_root=tmp_path,
                       resolution_index=index, capture_statements=capture)
    return parser.parse_file(ctx)


def _fn_calls(rec: FileRecord) -> dict[str, dict[str, str | None]]:
    return {f.name: {c.name: c.path for c in f.calls} for f in rec.functions}


def _class_calls(rec: FileRecord) -> dict[str, dict[str, str | None]]:
    return {c.name: {k.name: k.path for k in c.calls} for c in rec.classes if c.calls}


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


def test_field_initializer_calls_are_the_class_calls(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/ApiHandlerBuilder.java")
    assert _class_calls(rec) == {
        "ApiHandlerBuilder": {"ApiConfig": f"{_H}/ApiHandler.java", "norm": f"{_H}/Util.java"},
    }
    assert _fn_calls(rec)["ApiHandlerBuilder"] == {}  # the constructor's own body: unchanged


def test_a_field_stored_anonymous_subclass_calls_from_its_class(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/DataWriteApiHandler.java")
    # `errorAlert = new ResponseHandler() { … }`: the enclosing class runs its constructor
    assert _class_calls(rec)["DataWriteApiHandler"] == {"ResponseHandler": f"{_H}/ApiHandler.java"}


def test_static_block_calls_are_the_class_calls(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/ServiceServer.java")
    server = next(c for c in rec.classes if c.name == "ServiceServer")
    # every call of the block, nested statements included, once per name in order of appearance;
    # `new HashMap<>()`: a library class, no call
    assert [(c.name, c.path) for c in server.calls or []] == [
        ("put", None),
        ("WriteHandler", f"{_H}/WriteHandler.java"),
        ("isEmpty", None),
        ("add", f"{_H}/Registry.java"),
    ]
    assert _fn_calls(rec)["start"] == {}


def test_enum_constant_argument_calls_are_the_enum_calls(tmp_path: Path) -> None:
    assert _class_calls(_parse(tmp_path, "h/Client.java")) == {
        "KIND": {"WriteHandler": f"{_H}/WriteHandler.java"},  # `Client` itself: none, no `calls`
    }


def test_an_anonymous_class_initializer_calls_are_its_own(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/Defaults.java")
    anon = next(c for c in rec.classes if c.isAnonymous)
    assert {c.name: c.path for c in anon.calls or []} == {
        "put": None, "WriteHandler": f"{_H}/WriteHandler.java",
    }
    assert _fn_calls(rec)["make"] == {}  # a JDK class built: the method calls nothing of its own


def test_class_calls_do_not_depend_on_statement_capture(tmp_path: Path) -> None:
    on = _class_calls(_parse(tmp_path, "h/ServiceServer.java"))
    off = _parse(tmp_path, "h/ServiceServer.java", capture=False)
    assert off.statements == [] and _class_calls(off) == on


def test_statements_carry_no_calls(tmp_path: Path) -> None:
    rec = _parse(tmp_path, "h/ServiceServer.java")
    assert all("calls" not in s.model_dump(exclude_none=True) for s in rec.statements)
