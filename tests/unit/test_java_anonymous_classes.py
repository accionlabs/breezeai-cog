"""Java anonymous classes (``new T(…) { … }``): each is a Class with its own methods, and the
method or field that creates it keeps only its own calls and statements. One stored in a field is
named after the field and stands for its declaration; any other is numbered ``Outer$n``."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from breezeai_cog.emit import to_line
from breezeai_cog.parsers.base import ParseContext
from breezeai_cog.parsers.java.parser import JavaParser
from breezeai_cog.schemas import FileRecord

_REL = "app/DataWriteApiHandler.java"
_SRC = """package app;

public class DataWriteApiHandler extends NoticeApiHandler {

  private final ResponseHandler errorAlertResponse = new ResponseHandler() {
    private Message<JsonObject> resp;
    @Override
    public void handle(ResponseData response) {
      handleError(resp.body(), apiName().toString(), method, date, place, null, null, uri, id);
    }
    @Override
    public void beforeHandle(Message<JsonObject> response) { this.resp = response; }
  };

  @Override
  protected ResponseData createResponseData(int code, Object body) {
    return new ResponseData(code, body) {
      @Override
      public JsonArray toArray() {
        JsonObject obj = toObject();
        JsonElement elem = obj.getElement("data");
        return (elem != null && elem.isArray()) ? elem.asArray() : null;
      }
    };
  }

  void start() {
    bus.registerHandler(addr, new Handler<Message>() {
      public void handle(Message m) {
        process(m);
        Runnable r = new Runnable() { public void run() { inner(); } };
      }
    });
    list.forEach(x -> x.go());
  }

  interface Listener { void on(); }

  void listen() {
    add(new Listener() { public void on() { fire(); } });
  }

  static class Nested {
    void m() { use(new Runnable() { public void run() { nested(); } }); }
  }
}
"""


def _parse(tmp_path: Path, src: str = _SRC, *, capture: bool = True) -> FileRecord:
    p = tmp_path / _REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src)
    parser = JavaParser()
    ctx = ParseContext(
        path=_REL,
        abs_path=p,
        source=src.encode(),
        repo_root=tmp_path,
        resolution_index=parser.build_index(tmp_path, [p]),
        capture_statements=capture,
    )
    return parser.parse_file(ctx)


def _cid(name: str) -> str:
    return f"{_REL}#{name}"


def _fn(record: FileRecord, owner: str, name: str):
    return next(f for f in record.functions if f.parentId == _cid(owner) and f.name == name)


def _calls(record: FileRecord, owner: str, name: str) -> list[str]:
    return [c.name for c in _fn(record, owner, name).calls]


def test_a_field_initializer_anonymous_class_is_named_after_its_field(tmp_path: Path) -> None:
    record = _parse(tmp_path)
    cls = next(c for c in record.classes if c.id == _cid("DataWriteApiHandler.errorAlertResponse"))
    assert cls.name == "errorAlertResponse"
    assert cls.parentId == _REL  # like every nested type, contained by the file
    assert cls.isAnonymous is True
    assert cls.metadata is None
    assert (cls.startLine, cls.endLine) == (5, 13)  # the whole field declaration
    assert "handleError" in _calls(record, "DataWriteApiHandler.errorAlertResponse", "handle")
    assert (
        _fn(record, "DataWriteApiHandler.errorAlertResponse", "beforeHandle").id
        == f"{_REL}#DataWriteApiHandler.errorAlertResponse#beforeHandle@11"
    )  # from its annotation


def test_a_field_storing_an_anonymous_class_is_no_statement(tmp_path: Path) -> None:
    record = _parse(tmp_path)
    # the Class is the declaration: it is not also a `field_declaration` statement
    assert not any(s.startLine == 5 for s in record.statements)
    own = [s for s in record.statements if s.parentId == _cid("DataWriteApiHandler.errorAlertResponse")]
    assert [s.text for s in own] == ["private Message<JsonObject> resp;"]  # its own field


def test_a_method_scope_anonymous_class_owns_its_calls(tmp_path: Path) -> None:
    record = _parse(tmp_path)
    assert _calls(record, "DataWriteApiHandler$1", "toArray") == [
        "toObject",
        "getElement",
        "isArray",
        "asArray",
    ]
    assert _calls(record, "DataWriteApiHandler", "createResponseData") == []
    ret = next(s for s in record.statements if s.startLine == 17)
    assert ret.parentId == _fn(record, "DataWriteApiHandler", "createResponseData").id
    assert ret.text == "return new ResponseData(code, body) { … };"
    in_body = {
        s.startLine
        for s in record.statements
        if s.parentId == _fn(record, "DataWriteApiHandler$1", "toArray").id
    }
    assert in_body == {20, 21, 22}


def test_constructor_arguments_make_the_type_a_superclass(tmp_path: Path) -> None:
    record = _parse(tmp_path)
    by_id = {c.id: c for c in record.classes}
    assert by_id[_cid("DataWriteApiHandler$1")].extends == "ResponseData"
    # no arguments and declared elsewhere: a class or an interface, so neither is set
    unknown = by_id[_cid("DataWriteApiHandler.errorAlertResponse")]
    assert (unknown.extends, unknown.implements) == (None, [])
    # declared in this file as an interface
    listener = by_id[_cid("DataWriteApiHandler$3")]
    assert (listener.extends, listener.implements) == (None, ["Listener"])


def test_an_anonymous_class_passed_as_an_argument(tmp_path: Path) -> None:
    record = _parse(tmp_path)
    assert _calls(record, "DataWriteApiHandler$2", "handle") == ["process"]
    # start() keeps its own calls; the lambda's stay with it (lambdas are unchanged)
    assert _calls(record, "DataWriteApiHandler", "start") == ["registerHandler", "forEach", "go"]
    reg = next(s for s in record.statements if s.startLine == 28)
    assert reg.text == "bus.registerHandler(addr, new Handler<Message>() { … });"
    lam = next(s for s in record.statements if s.startLine == 34)
    assert lam.parentId == _fn(record, "DataWriteApiHandler", "start").id


def test_a_nested_anonymous_class(tmp_path: Path) -> None:
    record = _parse(tmp_path)
    inner = next(c for c in record.classes if c.id == _cid("DataWriteApiHandler$2$1"))
    assert (inner.isAnonymous, inner.implements) == (True, ["Runnable"])  # java.lang
    assert _calls(record, "DataWriteApiHandler$2$1", "run") == ["inner"]
    assert "inner" not in _calls(record, "DataWriteApiHandler$2", "handle")


def test_a_member_type_numbers_its_own_anonymous_classes(tmp_path: Path) -> None:
    record = _parse(tmp_path)
    assert _calls(record, "DataWriteApiHandler.Nested$1", "run") == ["nested"]
    assert _calls(record, "DataWriteApiHandler.Nested", "m") == ["use"]
    nested = next(c for c in record.classes if c.id == _cid("DataWriteApiHandler.Nested$1"))
    assert nested.name == "Nested$1"


def test_ids_are_identical_across_runs(tmp_path: Path) -> None:
    first, second = _parse(tmp_path / "a"), _parse(tmp_path / "b")
    assert [c.id for c in first.classes] == [c.id for c in second.classes]
    assert [f.id for f in first.functions] == [f.id for f in second.functions]
    assert [s.id for s in first.statements] == [s.id for s in second.statements]


def test_named_classes_and_functions_are_unchanged(tmp_path: Path) -> None:
    record = _parse(tmp_path)
    named = sorted(c.id for c in record.classes if not c.isAnonymous)
    assert named == [
        _cid("DataWriteApiHandler"),
        _cid("DataWriteApiHandler.Listener"),
        _cid("DataWriteApiHandler.Nested"),
    ]


def test_methods_are_captured_without_statement_capture(tmp_path: Path) -> None:
    record = _parse(tmp_path, capture=False)
    assert record.statements == []
    assert "handleError" in _calls(record, "DataWriteApiHandler.errorAlertResponse", "handle")


def test_sql_only_in_the_anonymous_body_is_its_own(tmp_path: Path) -> None:
    src = """package app;
public class Repo {
  void load() {
    run(new Job() {
      public void exec() { String q = "SELECT id FROM orders WHERE x = 1"; }
    });
  }
}
"""
    record = _parse(tmp_path, src)
    call = next(s for s in record.statements if s.startLine == 4)
    assert call.semanticType is None
    q = next(s for s in record.statements if s.startLine == 5)
    assert q.semanticType == "query_statement"
    assert q.parentId == f"{_REL}#Repo$1#exec@5"


def test_output_validates(tmp_path: Path) -> None:
    record = _parse(tmp_path)
    errors = list(
        Draft202012Validator(FileRecord.model_json_schema(by_alias=True)).iter_errors(
            json.loads(to_line(record))
        )
    )
    assert not errors, errors


def test_a_type_declared_in_an_anonymous_class(tmp_path: Path) -> None:
    src = """package app;
public class Ctl {
  void m() {
    strip(new Stripper() {
      class Line { String text; Line(String t) { this.text = t; } }
      void write(String s) { keep(new Line(s)); }
    });
  }
}
"""
    record = _parse(tmp_path, src)
    line = next(c for c in record.classes if c.id == f"{_REL}#Ctl$1.Line")
    assert (line.name, line.parentId) == ("Line", _REL)
    assert {s.text for s in record.statements if s.parentId == line.id} == {"String text;"}
    assert _calls(record, "Ctl$1", "write") == ["keep"]
    assert _calls(record, "Ctl", "m") == ["strip"]


# --- what the instantiated type is declared as: extends, implements or neither ----------------


def _repo(tmp_path: Path, files: dict[str, str], target: str) -> FileRecord:
    for rel, src in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    parser = JavaParser()
    p = tmp_path / target
    ctx = ParseContext(
        path=target,
        abs_path=p,
        source=p.read_bytes(),
        repo_root=tmp_path,
        resolution_index=parser.build_index(tmp_path, sorted(tmp_path.rglob("*.java"))),
    )
    return parser.parse_file(ctx)


_KINDS = """package app;
import lib.Handler;
import java.util.*;
public class Svc {
  void m() {
    a(new Listener() { public void on() {} });
    a(new Task() { public void go() {} });
    a(new Handler<String>() { public void handle(String s) {} });
    a(new Runnable() { public void run() {} });
    a(new Comparator<String>() { public int compare(String x, String y) { return 0; } });
    a(new java.util.TimerTask() { public void run() {} });
    a(new other.Base() { });
  }
}
"""


def _heritage_of(tmp_path: Path) -> dict[str, tuple[str | None, list[str]]]:
    record = _repo(tmp_path, {
        "app/Svc.java": _KINDS,
        "app/Listener.java": "package app;\npublic interface Listener { void on(); }\n",
        "app/Task.java": "package app;\npublic abstract class Task { abstract void go(); }\n",
        "other/Base.java": "package other;\npublic class Base { }\n",
    }, "app/Svc.java")
    return {c.name: (c.extends, c.implements) for c in record.classes if c.isAnonymous}


def test_a_repository_type_in_another_file_gives_extends_or_implements(tmp_path: Path) -> None:
    kinds = _heritage_of(tmp_path)
    assert kinds["Svc$1"] == (None, ["Listener"])  # an interface of the same package
    assert kinds["Svc$2"] == ("Task", [])  # an abstract class, no constructor arguments
    assert kinds["Svc$7"] == ("other.Base", [])  # written fully qualified


def test_a_library_type_gives_neither(tmp_path: Path) -> None:
    # `lib.Handler` is no repository type and not a known JDK one: a class or an interface
    assert _heritage_of(tmp_path)["Svc$3"] == (None, [])


def test_known_jdk_types_give_extends_or_implements(tmp_path: Path) -> None:
    kinds = _heritage_of(tmp_path)
    assert kinds["Svc$4"] == (None, ["Runnable"])  # java.lang
    assert kinds["Svc$5"] == (None, ["Comparator<String>"])  # import java.util.*
    assert kinds["Svc$6"] == ("java.util.TimerTask", [])


def test_a_repository_type_hides_the_jdk_one_of_its_name(tmp_path: Path) -> None:
    record = _repo(tmp_path, {
        "app/Svc.java": "package app;\npublic class Svc {\n"
                        "  void m() { a(new Runnable() { public void run() {} }); }\n}\n",
        "app/Runnable.java": "package app;\npublic abstract class Runnable { }\n",
    }, "app/Svc.java")
    anon = next(c for c in record.classes if c.isAnonymous)
    assert (anon.extends, anon.implements) == ("Runnable", [])  # app.Runnable, a class


# --- a field's anonymous class ------------------------------------------------------------------


_FIELDS = """package app;
public class Svc {
  @Inject
  private final Runnable first = new Runnable() { public void run() { one(); } };
  Runnable a = new Runnable() { public void run() {} },
           b = new Runnable() { public void run() {} };
  Runnable c = new Runnable() { public void run() {} }, d = null;
  Object e = wrap(new Runnable() { public void run() {} });
  void m() { a(new Runnable() { public void run() {} }); }
}
"""


def test_a_field_class_carries_the_declaration(tmp_path: Path) -> None:
    record = _parse(tmp_path, _FIELDS)
    first = next(c for c in record.classes if c.id == _cid("Svc.first"))
    assert (first.startLine, first.endLine) == (3, 4)  # from its annotation
    assert [d.name for d in first.decorators] == ["Inject"]
    assert _calls(record, "Svc.first", "run") == ["one"]


def test_each_declarator_of_a_field_is_its_own_class(tmp_path: Path) -> None:
    record = _parse(tmp_path, _FIELDS)
    by_name = {c.name: c for c in record.classes if c.isAnonymous}
    assert (by_name["a"].startLine, by_name["b"].startLine) == (5, 6)
    assert not any(s.startLine == 5 for s in record.statements)  # stores only anonymous classes


def test_a_declaration_storing_something_else_stays_a_statement(tmp_path: Path) -> None:
    record = _parse(tmp_path, _FIELDS)
    assert {c.name for c in record.classes if c.isAnonymous} == {"first", "a", "b", "c", "Svc$1", "Svc$2"}
    texts = {s.startLine: s.text for s in record.statements}
    assert texts[7] == "Runnable c = new Runnable() { … }, d = null;"
    # inside a larger expression: numbered, and the declaration is a statement
    assert texts[8] == "Object e = wrap(new Runnable() { … });"
    assert next(c for c in record.classes if c.name == "Svc$1").startLine == 8
    assert next(c for c in record.classes if c.name == "Svc$2").startLine == 9
